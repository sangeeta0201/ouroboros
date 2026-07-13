# loop-orchestrator

A two-model autonomous loop for long-running engineering tasks, built on the
[`claude`](https://docs.claude.com/en/docs/claude-code) CLI.

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
| `orchestrator.py` | The loop (worker + guide driver). |
| `view.py` | Live REPL-style trace viewer. |
| `watch.sh` | Plain-text status dashboard. |
| `task.example.txt` | Task-file template. |
| `constraints.example.txt` | Constraints-file template. |
