#!/usr/bin/env python3
"""Live Claude-Code-style viewer for the orchestrator's worker stream.

Tails the newest iterNN_worker.stream.jsonl in the latest run dir and renders
events the way the Claude Code REPL does: assistant text, `● Tool(input)` calls,
and `⎿ result` blocks. Auto-follows into the next iteration's file when the loop
advances. Ctrl-C to quit.

Usage:  python3 view.py            # follow newest run
        python3 view.py <run_dir>  # follow a specific run dir
"""
from __future__ import annotations
import glob, json, os, sys, time

RUNS = "/home/claudeuser/loop-orchestrator/runs"

# ANSI
DIM = "\033[2m"; BOLD = "\033[1m"; RST = "\033[0m"
CYAN = "\033[36m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAG = "\033[35m"; RED = "\033[31m"


def latest_run() -> str:
    dirs = sorted(glob.glob(os.path.join(RUNS, "*/")), key=os.path.getmtime)
    if not dirs:
        sys.exit("no run dirs yet")
    return dirs[-1]


def iter_files(run: str) -> list[str]:
    return sorted(glob.glob(os.path.join(run, "iter*_worker.stream.jsonl")))


def clip(s: str, n: int = 500) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def render(ev: dict) -> None:
    t = ev.get("type")
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []):
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                print(f"\n{b['text'].strip()}\n")
            elif bt == "tool_use":
                name = b.get("name", "?")
                inp = b.get("input", {})
                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or json.dumps(inp, default=str)
                print(f"{GREEN}●{RST} {BOLD}{name}{RST}({CYAN}{clip(str(arg),300)}{RST})")
    elif t == "user":
        # tool results come back as user turns
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "tool_result":
                c = b.get("content", "")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                out = clip(str(c), 400)
                color = RED if b.get("is_error") else DIM
                print(f"  {color}⎿ {out}{RST}")
    elif t == "result":
        cost = ev.get("total_cost_usd")
        dur = ev.get("duration_ms")
        extra = []
        if isinstance(cost, (int, float)): extra.append(f"${cost:.4f}")
        if isinstance(dur, (int, float)): extra.append(f"{dur/1000:.0f}s")
        print(f"\n{MAG}── turn done {' '.join(extra)} ──{RST}\n")


def follow(run: str) -> None:
    print(f"{YELLOW}watching {run}{RST}  (Ctrl-C to quit)\n")
    seen: set[str] = set()
    pos: dict[str, int] = {}
    while True:
        files = iter_files(run)
        # allow loop to roll to a newer run dir
        newer = latest_run()
        if os.path.abspath(newer) != os.path.abspath(run) and iter_files(newer):
            run = newer
            print(f"\n{YELLOW}→ new run {run}{RST}\n")
            seen.clear(); pos.clear()
            continue
        for f in files:
            base = os.path.basename(f)
            if base not in seen:
                seen.add(base)
                print(f"\n{BOLD}{YELLOW}===== {base} ====={RST}")
                pos[f] = 0
            try:
                with open(f) as fh:
                    fh.seek(pos.get(f, 0))
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            render(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                    pos[f] = fh.tell()
            except FileNotFoundError:
                pass
        time.sleep(1.5)


if __name__ == "__main__":
    run = sys.argv[1] if len(sys.argv) > 1 else latest_run()
    try:
        follow(run)
    except KeyboardInterrupt:
        print("\nbye")
