#!/usr/bin/env python3
"""
Two-model loop orchestrator.

  WORKER = Claude Code headless (`claude -p`) -- does the actual engineering work.
  GUIDE  = a SECOND Claude Code process (`claude -p` with a different model +
           an advisor system prompt) -- answers the worker's questions and steers
           it, so no human sits in the interview seat.

Both worker and guide run through the local `claude` CLI, so they automatically use
whatever auth/gateway the CLI is configured with (here: the AMD LLM gateway). No raw
API key handling is required, and Claude Code applies prompt caching internally.

Protocol: the worker is told (via --append-system-prompt) never to ask the human.
Instead it must end EVERY turn with exactly one control line, as the LAST line:

    <<TASK_COMPLETE>> <final summary>       # nothing left to do  -> loop stops
    <<GUIDE_NEEDED>> <question / decision>  # needs a ruling       -> ask the guide
    <<CONTINUE>> <what it will do next>     # more work, no ruling -> nudge to continue

When the worker asks for guidance, the orchestrator forwards the question to the GUIDE
(a persistent guide session, so it remembers prior rulings) and feeds the guide's
ruling back into the worker's session with --resume.

Usage:
    python3 orchestrator.py \
        --task "Refactor the X module and add tests" \
        --workdir /home/claudeuser/mirage \
        --constraints constraints.example.txt \
        --guide-model Claude-Opus-4.6 \
        --max-iters 30

See `python3 orchestrator.py --help` for all flags.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------- #
# Control-line protocol
# ----------------------------------------------------------------------------- #
MARKER_RE = re.compile(
    r"<<(TASK_COMPLETE|GUIDE_NEEDED|CONTINUE)>>[ \t]*(.*)", re.IGNORECASE
)

WORKER_PROTOCOL = """\
You are the WORKER in an automated two-model loop. A separate GUIDE model plays the
role a human normally would: it answers your questions and makes decisions for you.

RULES:
1. NEVER call the AskUserQuestion tool and never address a human. There is no human.
2. Make reasonable, decisive progress every turn. Do the work.
3. End EVERY response with exactly ONE control line, as the LAST line, verbatim:
     <<TASK_COMPLETE>> <one-line summary>      when the whole task is truly finished
     <<GUIDE_NEEDED>> <your specific question> when you genuinely need a decision
     <<CONTINUE>> <what you will do next turn>  when there is more work and no blocker
4. Prefer <<CONTINUE>> over <<GUIDE_NEEDED>>. Only ask the guide when a real fork in
   the road would waste significant work if you guessed wrong.
5. When you receive a GUIDE RULING, treat it as a binding instruction from the human
   and act on it immediately.
6. Obey every rule in the CONSTRAINTS section below without exception.
"""

GUIDE_PROTOCOL = """\
You are the GUIDE in an automated two-model engineering loop. A WORKER (an autonomous
coding agent) is executing a task. When the worker hits a real decision point it asks
you a question. You stand in for the human owner of the project.

Your job:
- Give a SINGLE, concrete, decisive instruction. Never bounce the question back.
- Keep the worker moving toward completion; avoid scope creep.
- Enforce the CONSTRAINTS strictly. If the worker proposes something that violates a
  constraint, forbid it and tell it what to do instead.
- Be brief: a short directive, not an essay. No preamble, no questions of your own.
- Answer from reasoning alone. Do NOT use any tools.
"""


# ----------------------------------------------------------------------------- #
# Shared: run one `claude -p` turn and return parsed JSON
# ----------------------------------------------------------------------------- #
def run_claude(
    prompt: str,
    *,
    cwd: str,
    system_suffix: str,
    model: str | None,
    permission_mode: str | None,
    dangerously_skip: bool,
    session_id: str | None,
    timeout: int,
    stream_log: Path | None = None,
    session_holder: list[str] | None = None,
) -> dict:
    """Run one `claude -p` turn.

    If stream_log is given, use stream-json so the turn's activity (assistant text +
    tool calls) is written live to <stream_log> (raw JSONL) and <stream_log>.log
    (human-readable), letting an outside `tail -f` watch what the worker is doing.
    Otherwise use plain json (used for the guide).
    """
    fmt = (["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
           if stream_log else ["--output-format", "json"])
    cmd = ["claude", "-p", prompt, *fmt, "--append-system-prompt", system_suffix]
    if model:
        cmd += ["--model", model]
    if dangerously_skip:
        cmd += ["--dangerously-skip-permissions"]
    elif permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if session_id:
        cmd += ["--resume", session_id]

    if stream_log is None:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"`claude` exited {proc.returncode}\nSTDERR:\n{proc.stderr}\n"
                f"STDOUT:\n{proc.stdout[:2000]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"could not parse claude JSON: {e}\nSTDOUT:\n{proc.stdout[:2000]}")

    return _run_streaming(cmd, cwd=cwd, timeout=timeout, stream_log=stream_log,
                          session_holder=session_holder)


def _run_streaming(cmd: list[str], *, cwd: str, timeout: int, stream_log: Path,
                   session_holder: list[str] | None = None) -> dict:
    """Run claude with --output-format stream-json, teeing live activity to files."""
    final: dict = {}
    human_path = stream_log.with_suffix(".log")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    # Watchdog: hard-kill if the turn exceeds the timeout (even if it emits no lines).
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        with open(stream_log, "w") as raw, open(human_path, "w") as human:
            for line in proc.stdout:  # type: ignore[union-attr]
                raw.write(line)
                raw.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_holder is not None and ev.get("session_id"):
                    session_holder[:] = [ev["session_id"]]
                etype = ev.get("type")
                if etype == "assistant":
                    for b in ev.get("message", {}).get("content", []):
                        bt = b.get("type")
                        if bt == "text" and b.get("text", "").strip():
                            human.write(f"[assistant] {b['text'].strip()}\n")
                        elif bt == "tool_use":
                            inp = json.dumps(b.get("input", {}), default=str)
                            if len(inp) > 300:
                                inp = inp[:300] + "…"
                            human.write(f"  [tool] {b.get('name')} {inp}\n")
                    human.flush()
                elif etype == "result":
                    final = ev
    finally:
        watchdog.cancel()
    proc.wait()
    if proc.returncode and proc.returncode < 0:
        raise subprocess.TimeoutExpired(cmd, timeout)
    if not final:
        raise RuntimeError(
            f"streaming turn produced no result event (exit {proc.returncode}); "
            f"see {human_path}"
        )
    return final


def parse_control(text: str) -> tuple[str, str]:
    """Return (KIND, payload) from the LAST control marker; default CONTINUE."""
    matches = list(MARKER_RE.finditer(text or ""))
    if not matches:
        return ("CONTINUE", "")
    m = matches[-1]
    return (m.group(1).upper(), m.group(2).strip())


# ----------------------------------------------------------------------------- #
# Guide turn
# ----------------------------------------------------------------------------- #
def ask_guide(
    *,
    task: str,
    constraints: str,
    question: str,
    recent_worker_output: str,
    model: str,
    guide_cwd: str,
    session_id: str | None,
    timeout: int,
) -> tuple[str, str | None]:
    system = (
        GUIDE_PROTOCOL
        + f"\n\nOVERALL TASK:\n{task}\n\nCONSTRAINTS:\n{constraints or '(none)'}\n"
    )
    prompt = (
        "The worker's most recent output (tail):\n"
        "----------------------------------------\n"
        f"{recent_worker_output[-4000:]}\n"
        "----------------------------------------\n\n"
        f"The worker is asking:\n{question}\n\n"
        "Give it one concrete, binding instruction. Answer only; do not use tools."
    )
    data = run_claude(
        prompt,
        cwd=guide_cwd,
        system_suffix=system,
        model=model,
        permission_mode="plan",       # advisory only: no edits
        dangerously_skip=False,
        session_id=session_id,
        timeout=timeout,
    )
    ruling = (data.get("result", "") or "").strip() or "(guide returned no text)"
    return ruling, data.get("session_id", session_id)


# ----------------------------------------------------------------------------- #
# Orchestration loop
# ----------------------------------------------------------------------------- #
def log(run_dir: Path, name: str, content: str) -> None:
    (run_dir / name).write_text(content)


def main() -> int:
    ap = argparse.ArgumentParser(description="Two-model (worker + guide) loop orchestrator.")
    ap.add_argument("--task", help="The task for the worker. Or use --task-file.")
    ap.add_argument("--task-file", help="Path to a file containing the task prompt.")
    ap.add_argument("--constraints", help="Path to a constraints file (things the worker must never do).")
    ap.add_argument("--workdir", default=os.getcwd(), help="Directory to run the worker in.")
    ap.add_argument("--worker-model", default=None, help="Worker model (default: CLI default, e.g. ANTHROPIC_MODEL).")
    ap.add_argument("--guide-model", default="Claude-Opus-4.6", help="Guide model (gateway name).")
    ap.add_argument("--max-iters", type=int, default=30, help="Max worker turns before giving up.")
    ap.add_argument("--worker-timeout", type=int, default=3600, help="Per-turn worker timeout (seconds). Slow ATT captures need headroom.")
    ap.add_argument("--guide-timeout", type=int, default=180, help="Per-call guide timeout (seconds).")
    ap.add_argument("--max-consecutive-timeouts", type=int, default=3,
                    help="Stop only after this many turns time out in a row (a single timeout resumes).")
    ap.add_argument("--resume-session", default=None,
                    help="Resume an existing worker session id instead of starting fresh (keeps prior context).")
    ap.add_argument(
        "--permission-mode",
        default="acceptEdits",
        choices=["default", "acceptEdits", "plan", "bypassPermissions"],
        help="Worker permission mode (ignored if --yolo).",
    )
    ap.add_argument(
        "--yolo",
        action="store_true",
        help="Pass --dangerously-skip-permissions to the WORKER (fully autonomous, riskier).",
    )
    args = ap.parse_args()

    if args.task_file:
        task = Path(args.task_file).read_text().strip()
    elif args.task:
        task = args.task
    else:
        ap.error("provide --task or --task-file")
        return 2

    constraints = Path(args.constraints).read_text().strip() if args.constraints else ""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(__file__).parent / "runs" / stamp
    guide_cwd = run_dir / "guide_cwd"
    guide_cwd.mkdir(parents=True, exist_ok=True)
    print(f"[loop] logging to {run_dir}")
    log(run_dir, "TASK.txt", task)
    if constraints:
        log(run_dir, "CONSTRAINTS.txt", constraints)

    worker_system = WORKER_PROTOCOL + f"\n\nCONSTRAINTS:\n{constraints or '(none)'}\n"

    next_prompt = task
    worker_session: str | None = args.resume_session
    guide_session: str | None = None
    consecutive_timeouts = 0

    for i in range(1, args.max_iters + 1):
        print(f"\n[loop] === iteration {i}/{args.max_iters} ===")
        first_line = (next_prompt or "").strip().splitlines()[0] if next_prompt else ""
        print(f"[loop] worker <- {first_line[:200]} ...")
        stream_log = run_dir / f"iter{i:02d}_worker.stream.jsonl"
        print(f"[loop] live activity -> {stream_log.with_suffix('.log')}", flush=True)
        session_holder: list[str] = []
        try:
            result = run_claude(
                next_prompt,
                cwd=args.workdir,
                system_suffix=worker_system,
                model=args.worker_model,
                permission_mode=args.permission_mode,
                dangerously_skip=args.yolo,
                session_id=worker_session,
                timeout=args.worker_timeout,
                stream_log=stream_log,
                session_holder=session_holder,
            )
        except subprocess.TimeoutExpired:
            consecutive_timeouts += 1
            # Even a killed turn tells us its session id via the streamed init event,
            # so we can RESUME instead of throwing away the whole run.
            if session_holder:
                worker_session = session_holder[0]
            print(f"[loop] worker TIMED OUT after {args.worker_timeout}s "
                  f"(consecutive {consecutive_timeouts}/{args.max_consecutive_timeouts}); "
                  f"resuming session {worker_session} to continue.", flush=True)
            if consecutive_timeouts >= args.max_consecutive_timeouts:
                print("[loop] too many consecutive timeouts; stopping.")
                return 1
            next_prompt = (
                "Your previous turn was cut off by the per-turn time limit before you could "
                "emit a control line. You very likely have PARTIAL progress already on disk "
                "(benchmark output files, traces, edits, notes). Do NOT restart from scratch: "
                "first inspect what is already done, then continue from there. From now on keep "
                "each turn SHORT — run at most ONE long-running operation (a single benchmark "
                "sweep, build, or ATT capture) per turn, then STOP and end with <<CONTINUE>> so "
                "your progress is checkpointed. End this turn with exactly one control line."
            )
            continue
        except RuntimeError as e:
            print(f"[loop] worker error:\n{e}")
            return 1

        consecutive_timeouts = 0
        worker_session = result.get("session_id", worker_session)
        worker_text = result.get("result", "") or ""
        cost = result.get("total_cost_usd")
        log(run_dir, f"iter{i:02d}_worker.txt", worker_text)
        print(f"[loop] worker -> {len(worker_text)} chars"
              + (f", cost ${cost:.4f}" if isinstance(cost, (int, float)) else ""))

        kind, payload = parse_control(worker_text)
        print(f"[loop] control: <<{kind}>> {payload[:160]}")

        if kind == "TASK_COMPLETE":
            print(f"\n[loop] DONE after {i} iteration(s). Summary: {payload}")
            log(run_dir, "SUMMARY.txt", payload)
            return 0

        if kind == "GUIDE_NEEDED":
            print("[loop] consulting guide model ...")
            try:
                ruling, guide_session = ask_guide(
                    task=task,
                    constraints=constraints,
                    question=payload or worker_text,
                    recent_worker_output=worker_text,
                    model=args.guide_model,
                    guide_cwd=str(guide_cwd),
                    session_id=guide_session,
                    timeout=args.guide_timeout,
                )
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                print(f"[loop] guide error:\n{e}")
                return 1
            log(run_dir, f"iter{i:02d}_guide.txt", f"Q: {payload}\n\nRULING:\n{ruling}")
            print(f"[loop] guide -> {ruling[:200]}")
            next_prompt = (
                "GUIDE RULING (treat as a binding human instruction, act on it now):\n"
                f"{ruling}\n\n"
                "Proceed. Remember to end with one control line."
            )
        else:  # CONTINUE or missing marker
            next_prompt = (
                "Continue with the task. If you are truly finished, end with "
                "<<TASK_COMPLETE>>. If you need a decision, end with <<GUIDE_NEEDED>>. "
                "Otherwise keep going and end with <<CONTINUE>>."
            )

    print(f"\n[loop] hit max-iters ({args.max_iters}) without TASK_COMPLETE.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
