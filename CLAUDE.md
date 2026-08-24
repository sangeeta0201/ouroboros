# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Ouroboros is a two-model autonomous loop: a **worker** does the engineering while a second
**guide** model (different model, advisory-only) stands in for the human, answering the worker's
questions so an unattended run never stalls on a question nobody is there to answer.

There are **two independent implementations of that idea in this repo**, and they share no code:

1. **`orchestrator.py`** — a standalone Python driver. Both worker and guide are plain `claude -p`
   subprocesses. No SDK/API dependency, no package manifest, no test suite.
2. **`/ouro`** — the same idea native to Claude Code. The worker is a Claude Code *subagent*
   (so its own agentic loop replaces the `<<CONTINUE>>` protocol entirely) and the guide is driven
   by `.claude/ouro/guide.sh`. Installed into `~/.claude` via `install.sh`.

When changing one, do not assume the other needs the same change — the protocols genuinely differ
(see the two Architecture sections below).

## Commands

There is no build, lint, or test tooling — this is a single dependency-free Python 3 script repo.

Run the loop:
```bash
python3 orchestrator.py --task-file task.example.txt --constraints constraints.example.txt \
  --workdir /path/to/target/repo --guide-model Claude-Opus-4.6 --max-iters 40 --yolo
```

Run in the background and tail it:
```bash
nohup python3 orchestrator.py ... > orchestrator.console.log 2>&1 &
tail -f orchestrator.console.log
```

Watch a run live:
```bash
python3 view.py          # REPL-style renderer of the newest run's worker stream
./watch.sh                # plain-text dashboard (console tail + worker activity)
```

Manually sanity-check a change to `orchestrator.py` with a short, cheap task/constraints pair and a
low `--max-iters` before trusting it on a real long-running task.

Install / uninstall the `/ouro` half:
```bash
./install.sh              # symlink .claude/** into ~/.claude (repo stays source of truth)
./install.sh --copy       # detached copies
./install.sh --uninstall
```
**Restart Claude Code after installing** — slash commands hot-reload, agents do not. A newly added
agent is invisible to the running session, which shows up as `Agent type 'ouro-worker' not found`.

Exercise the guide directly when changing its protocol — far cheaper than a full run:
```bash
cd /path/to/scratch/repo
OURO_PLAN=$PWD/plans/foo.md ~/.claude/ouro/guide.sh ask    "<question>"; echo "exit=$?"
OURO_PLAN=$PWD/plans/foo.md ~/.claude/ouro/guide.sh review "<claimed work>"; echo "exit=$?"
```
Do not pipe those through `head`/`tail` while testing — the pipe swallows the exit code, which *is*
the contract.

## Architecture

### Control-line protocol (the core mechanism)

Every worker turn is forced (via `--append-system-prompt`, see `WORKER_PROTOCOL` in
[orchestrator.py](orchestrator.py)) to end with exactly one control line, matched by `MARKER_RE`:

- `<<CONTINUE>> <what's next>` — more work; the orchestrator sends a generic "keep going" nudge and
  resumes the same worker session via `claude --resume`.
- `<<GUIDE_NEEDED>> <question>` — the worker hit a decision fork. The orchestrator calls `ask_guide()`,
  which runs the guide as a *separate*, persistent `claude -p` session (`--permission-mode plan`, no
  tools, so it can only reason and answer — never edit). The guide's ruling is fed back into the
  worker's next prompt as a binding instruction.
- `<<TASK_COMPLETE>> <summary>` — the orchestrator exits after this turn.

The worker is explicitly told never to call `AskUserQuestion` — there is no human to answer it. The
guide exists specifically to absorb that role so long-running/unattended loops don't stall.

### Session continuity and timeouts

Both worker and guide sessions persist across turns via `--resume <session_id>` (captured from each
`claude -p` JSON/stream-json response), so context accumulates naturally instead of being re-sent.

Per-turn timeouts are *not* fatal: `run_claude()` / `_run_streaming()` hard-kills the subprocess after
`--worker-timeout` seconds via a `threading.Timer` watchdog, but the session id is recoverable even
from a killed stream (it's emitted early in the stream-json `init` event and captured into
`session_holder`), so the loop resumes the same session with a nudge to keep turns shorter. The loop
only gives up after `--max-consecutive-timeouts` timeouts *in a row* (a later successful turn resets
the counter).

### Two `run_claude()` output modes

- **Guide calls** use plain `--output-format json` (`run_claude` with `stream_log=None`): a single
  blocking `subprocess.run`, parsed as one JSON blob.
- **Worker calls** use `--output-format stream-json --include-partial-messages` via
  `_run_streaming()`: the subprocess is streamed line-by-line so a human can `tail -f` progress while
  it runs. Each line is written raw to `iterNN_worker.stream.jsonl` and a human-readable rendering
  (assistant text + `[tool] name input`) to the paired `.log` file. The final `result` event from the
  stream becomes the turn's return value.

### Constraints

The `--constraints` file's contents are injected verbatim into *both* the worker's system prompt
(`WORKER_PROTOCOL` + `CONSTRAINTS:`) and the guide's system prompt (`GUIDE_PROTOCOL` + `CONSTRAINTS:`),
so the guide can refuse worker proposals that violate a hard rule rather than relying on the worker to
police itself.

### Run artifacts

Each invocation creates `runs/<YYYYmmdd-HHMMSS>/` (relative to `orchestrator.py`'s own directory, not
`--workdir`) containing `TASK.txt`, `CONSTRAINTS.txt`, and per-iteration `iterNN_worker.stream.jsonl`
(+ `.log`), `iterNN_worker.txt` (final result text), and `iterNN_guide.txt` (Q + ruling) when the guide
was consulted. `runs/` is gitignored since transcripts may contain proprietary source/internal paths.

## Architecture — `/ouro` (Claude Code)

Deliberately **not** the control-line protocol. A subagent already loops until it decides it is
done, so there is no `<<CONTINUE>>`, no `MARKER_RE`, and no external resume driver. Only two
mechanisms carry over: a guide absorbing the questions, and a strict rule for interrupting the human.

### Verdicts live in exit codes

`guide.sh` is the whole control surface. The worker calls `ouro-guide ask|review` and **branches on
the exit code**, so it cannot forget to check for a sentinel buried in prose:

| Exit | Meaning |
|---|---|
| `0` | Ruling delivered, or review returned `OK:` |
| `10` | `ESCALATE:` — guide could not answer from the plan/docs; escalate to the human |
| `11` | `FIX:` — review found problems; fix and re-review |
| `3` | Guide unreachable after one retry; escalate rather than proceed alone |

Same principle throughout: anything that must be trustworthy goes in deterministic shell, and the
model's job is to audit it rather than produce it.

### The guide is read-only but grounded

`--tools "Read,Grep,Glob"` restricts capability at the source rather than asking for restraint, and
`--permission-mode bypassPermissions` then prevents a headless permission prompt from hanging the
call — safe precisely *because* the tool set contains nothing that writes. Session id is cached in
`.ouro/guide-session` and resumed, so the guide accumulates a memory of its own prior rulings and
of the worker's track record.

Unlike `orchestrator.py`'s tool-less guide, this one must ground every ruling in the plan,
`CLAUDE.md`, docs, or code — and reply `ESCALATE:` rather than invent an answer that isn't there.

### The guide reviews disk, not transcripts

Load-bearing and easy to get wrong: the guide reads the **repository**, so it reviews work done by
the worker, by a spawned subagent, or by a `Workflow` fan-out identically — it neither knows nor
needs to know who typed it. Delegation therefore creates **no review hole** and needs no second
reviewer. The genuine exception is a subagent's *findings* (research, summaries), which never touch
disk; those must be spot-checked by the worker before being built on.

### Confidence scoring

Every review reply carries `CONFIDENCE: <0-100>` plus `VERIFIED:` / `UNVERIFIED:` lists. It scores
**the worker's report**, not the guide's own certainty — an inverted reading here once produced 95
for a report the guide had just disproved, so the protocol names that failure explicitly and caps any
false claim at ≤39. The guide cannot execute anything, so "the tests passed" is always `UNVERIFIED`.
The worker must quote all three lines verbatim to the human, low scores included.

### Escalation contract

Five triggers only: guide punts, answer not in the plan/docs, no progress or looping, irreversible or
outward-facing action, blocked on access. Before escalating the worker must land all unblocked work,
then park — send one structured message and end the turn rather than guessing onward. Done is not an
escalation; it commits to the branch and never merges or pushes.

### Known limits (see README "Notes / limits")

- The worker cannot write into protected config dirs (e.g. `.claude/`) even with `--yolo` — Claude
  Code hard-protects its own config regardless of permission mode.
- `view.py` and `watch.sh` currently hardcode `/home/claudeuser/loop-orchestrator/runs` /
  `/home/claudeuser/mirage` — deployment-specific paths from the original host. Update these constants
  (`RUNS` in `view.py`, the paths in `watch.sh`) if using them from a different machine/checkout.
- `.claude/agents/ouro-worker.md` hardcodes `~/.claude/ouro/guide.sh`. That is why the repo layout
  mirrors the install layout exactly — `install.sh` is a 1:1 symlink. Installing anywhere other than
  `~/.claude` requires editing that path in the agent too.
- **`/ouro` itself has not been run end to end.** Every test went through
  `claude -p --agent ouro-worker`, which validates the worker protocol, the guide, escalation, and
  confidence scoring — but not the Agent-tool spawn, `isolation: "worktree"`, or
  `SendMessage(to: "main")`. Those three paths in `.claude/commands/ouro.md` are unexercised.
- The `Workflow` tool does **not** exist for subagents, so `/ouro` can never orchestrate one. Listing
  it in an agent's frontmatter `tools:` does not grant it — this was tested. It is available only to a
  headless top-level worker, and then only with `OURO_ALLOW_WORKFLOW=1`.

## Task/constraints files

Real task and constraints files (`task_*.txt`, `constraints_*.txt`) are gitignored because they
typically encode internal paths and proprietary details — only the `*.example.txt` templates are
committed. When writing a new task file, follow `task.example.txt`'s shape: OBJECTIVE, BACKGROUND
(what's already known, so the worker doesn't rediscover it), STEPS, a mandatory GATE before any
success claim, CONSTRAINTS, and a DELIVERABLE path inside `--workdir`.
