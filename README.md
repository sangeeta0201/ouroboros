# ouroboros

A self-sustaining, two-model autonomous loop
for long-running engineering tasks, built on the
[`claude`](https://docs.claude.com/en/docs/claude-code) CLI. The loop feeds itself:
one model does the work, another guides it, and no human sits in the middle.

Instead of a human sitting in the loop answering an agent's questions, a **worker**
model does the engineering while a second **guide** model plays the role of the
human advisor — it answers the worker's questions and rules on decision forks. Both
run as `claude -p` subprocesses, so whatever auth your `claude` CLI already has just
works.

```
             ┌──────────────┐   <<GUIDE_NEEDED>> question    ┌─────────────┐
  task ──────▶│    WORKER     │ ─────────────────────────────▶│    GUIDE     │
             │ (claude -p)   │◀───────────────────────────── │ (claude -p)  │
             └──────┬───────┘        ruling (via --resume)    └─────────────┘
                    │ <<CONTINUE>> / <<TASK_COMPLETE>>
                    ▼
             per-turn transcripts in runs/<timestamp>/
```

## Why ouroboros? (loop engineering)

This is an instance of **[loop engineering](https://addyosmani.com/blog/loop-engineering/)** —
the 2026 shift from hand-prompting an agent to *designing the loop that prompts the
agent*. As Boris Cherny, who leads Anthropic's Claude Code team, put it: *"I don't
prompt Claude anymore. I have loops running that prompt Claude and figuring out what
to do. My job is to write loops."* A hallmark of the pattern is **separating the model
that does the work from the one that judges/guides it**, so the writer is never its
own judge — exactly the worker/guide split here.

It also solves a real friction with today's frontier models: **they increasingly
interview you instead of just doing the work.** Anthropic's own
[interview technique](https://ai-checker.webcoda.com.au/articles/interview-technique-ai-requirements-gathering-2026)
has the model use `AskUserQuestion` to grill you for dozens of clarifications before
writing code, and research formalizes when agents *should* ask versus assume
([Ask or Assume?](https://arxiv.org/html/2603.26233v1),
[Curiosity by Design](https://arxiv.org/html/2507.21285v1)). That's excellent when a
human is at the keyboard — but it **stalls unattended, long-running work**: the agent
blocks on a question no one is there to answer. Ouroboros closes the gap by putting a
second model in the human's seat. The guide answers the worker's questions and rules
on forks, so the loop keeps turning overnight without you.

## How it works

Every worker turn ends with exactly one **control line**:

| Control line              | Meaning                                                        |
|---------------------------|---------------------------------------------------------------|
| `<<CONTINUE>>`            | More work to do — the loop resumes the same session next turn. |
| `<<GUIDE_NEEDED>> <q>`    | The worker hit a decision fork; the guide is asked `<q>` and its ruling is fed back via `--resume`. |
| `<<TASK_COMPLETE>>`       | Done. **The orchestrator exits after this turn.**             |

Session continuity is preserved with `claude --resume`, so the worker keeps full
context across turns. A per-turn timeout does **not** kill the loop: it resumes the
same session and nudges the worker to keep turns short; the loop only aborts after
`--max-consecutive-timeouts` in a row.

## Requirements

- The `claude` CLI, authenticated (`claude -p "hello"` should work).
- Python 3.

## Usage

1. Write a **task file** (see `task.example.txt`) — the objective, known context,
   step-by-step plan, the success gate, and where to write the deliverable.
2. Write a **constraints file** (see `constraints.example.txt`) — hard rules the
   worker must never break (the guide enforces them too).
3. Launch:

```bash
python3 orchestrator.py \
  --task-file task.example.txt \
  --constraints constraints.example.txt \
  --workdir /path/to/your/repo \
  --guide-model Claude-Opus-4.6 \
  --max-iters 40 \
  --worker-timeout 3600 \
  --max-consecutive-timeouts 3 \
  --yolo
```

Run it in the background and tail the console log:

```bash
nohup python3 orchestrator.py ... > orchestrator.console.log 2>&1 &
tail -f orchestrator.console.log
```

### Key flags

| Flag | Purpose |
|------|---------|
| `--task` / `--task-file` | The task prompt, inline or from a file. |
| `--constraints` | File of hard rules injected into the worker + enforced by the guide. |
| `--workdir` | Directory the worker runs in. |
| `--worker-model` / `--guide-model` | Model names (default: your CLI default for the worker). |
| `--max-iters` | Max worker turns before giving up. |
| `--worker-timeout` | Per-turn timeout in seconds (default 3600). A single expiry resumes, it does not abort. |
| `--max-consecutive-timeouts` | Abort only after this many timeouts in a row (default 3). |
| `--resume-session <id>` | Continue an existing worker session instead of starting fresh. |
| `--yolo` | Pass `--dangerously-skip-permissions` to the worker (full autonomy — riskier). |

## `/ouro` — the same loop, inside Claude Code

`orchestrator.py` exists because `claude -p` is one-shot: something external has to keep
resuming it. A Claude Code **subagent already runs its own multi-turn loop**, so inside
Claude Code the `<<CONTINUE>>` half of the protocol is unnecessary machinery. What's still
worth having is the *guide* — a second model absorbing the questions — and a strict rule
about when the loop is allowed to interrupt you.

That's `/ouro`. It is
[`superpowers:executing-plans`](https://github.com/obra/superpowers) with a guide model in
the human's seat:

```
/ouro plans/2026-08-13-retry.md     # execute that plan
/ouro                                # newest plan in docs/specs, plans/, specs/ …
```

A written plan is **required** — with nothing to ground rulings in, the guide would have to
invent answers, which is the failure this loop exists to prevent. The worker runs in a git
worktree on its own branch, consults the guide at decision forks and at mandatory review
checkpoints, and returns to your session only under five triggers: the guide punts, the
answer isn't in the plan or docs, it's looping, the action is irreversible or
outward-facing, or it's blocked on access. Done is not an interruption — it commits to the
branch and reports once. It never merges or pushes.

### How the guide differs here

`orchestrator.py`'s guide is deliberately tool-less and answers from reasoning alone. The
`/ouro` guide is **read-only over the repository** (`--tools "Read,Grep,Glob"`,
`--permission-mode plan`) so it can ground rulings in the plan, `CLAUDE.md`, the docs, and
the code — and refuse when the answer isn't there. It also reviews the worker's work at
checkpoints, so the writer is never its own judge.

Every review returns a **confidence score** describing how much of the worker's report the
repository actually corroborated, split into what the guide verified by reading and what it
could not check. The guide cannot execute anything, so claims like "the tests passed" always
land in `UNVERIFIED` — the score tells you whether re-running the work yourself is worth it.

### Install

```bash
./install.sh          # symlink into ~/.claude (repo stays source of truth)
./install.sh --copy   # detached copies instead
```

Then **restart Claude Code** — slash commands hot-reload, agents do not.

### Headless

The worker also runs standalone, which is the closest analogue to the `orchestrator.py`
flow:

```bash
OURO_ALLOW_WORKFLOW=1 claude -p --agent ouro-worker \
  --permission-mode bypassPermissions "Execute the plan at plans/foo.md …"
```

`OURO_ALLOW_WORKFLOW=1` is the one place multi-agent orchestration is permitted — the
`Workflow` tool is gated on human opt-in, and a headless worker has nobody to ask, so the
consent has to come from the launch command. Under `/ouro` the worker is a subagent and the
tool doesn't exist for it at all.

## Viewing the trace

Each run is logged under `runs/<timestamp>/`:

- `iterNN_worker.stream.log` — the worker's raw stream-json for turn NN.
- `iterNN_*` — guide questions/rulings and turn metadata.

Two viewers are included:

- **`view.py`** — a live, Claude-Code-REPL-style renderer of the worker stream:
  tool calls (`● Tool(input)`), tool results (`⎿ …`), and assistant text, colored,
  following the newest run and rolling into new iterations.

  ```bash
  python3 view.py
  ```

- **`watch.sh`** — a plain-text dashboard: console tail + current worker activity +
  whether a benchmark is running + GPU busy% + newest output files.

  ```bash
  ./watch.sh
  ```

You can also just `tail -f` the console log or any `runs/<ts>/iterNN_worker.stream.log`.

## Notes / limits

- **The worker cannot write into protected config dirs** (e.g. `.claude/`) even
  under `--yolo` — Claude Code hard-protects its own config. Have the task write
  deliverables into the working directory instead.
- `AskUserQuestion` can't be intercepted, so the task/constraints should instruct
  the worker to route all decisions to the guide (`<<GUIDE_NEEDED>>`), never to a
  human.

## Files

| File | Purpose |
|------|---------|
| `orchestrator.py` | The standalone loop (worker + guide driver). |
| `view.py` | Live REPL-style trace viewer. |
| `watch.sh` | Plain-text status dashboard. |
| `task.example.txt` | Task-file template. |
| `constraints.example.txt` | Constraints-file template. |
| `install.sh` | Symlink the `/ouro` files into `~/.claude`. |
| `.claude/commands/ouro.md` | The `/ouro` slash command. |
| `.claude/agents/ouro-worker.md` | The autonomous worker agent + escalation contract. |
| `.claude/ouro/guide.sh` | Guide driver — read-only session, verdict in the exit code. |
| `.claude/ouro/guide-protocol.md` | The guide's system prompt and confidence scoring. |
