---
description: Execute a written plan autonomously — a guide model answers the worker's questions, and you're only interrupted when both are genuinely stuck.
argument-hint: "[path/to/plan.md]"
---

Launch an autonomous ouroboros run against a written plan.

This is `superpowers:executing-plans` with a guide model in the human's seat. A background
worker agent executes the plan; when it hits a fork it consults a second, read-only model
instead of stopping to ask you. You are interrupted only when both models are out of road.

Plan argument (may be empty): $1

## 1. Resolve the plan — required, no guessing

If `$1` is given, use it. Otherwise find the newest plan in the first of these that exists:
`docs/superpowers/specs/`, `docs/plans/`, `plans/`, `specs/`, `docs/specs/` (look for
`*.md`, newest by mtime).

**If no plan file is found, stop and print exactly this — do not draft a plan, do not run
anything:**

```
✗ /ouro needs a written plan — none found.

  Nothing to ground the guide's rulings in means it would have to
  invent answers, which is what this loop exists to prevent.

  1. superpowers:brainstorming   → design + spec
  2. superpowers:writing-plans   → the plan
  3. /ouro <plan>                → execute it
```

If you found a plan by auto-discovery rather than argument, print which one and its mtime
before continuing, so a stale plan is obvious.

Read the plan. If it has a gap that would clearly block execution from step one, say so and
stop — that is cheaper to fix now than mid-run.

## 2. Isolate

Run `git rev-parse --is-inside-work-tree` in the working directory.

- **Git repo** → the worker gets `isolation: "worktree"`. Your checkout is untouched and
  you can keep working in it.
- **Not a git repo** → there is no worktree to isolate into and no branch to review, so the
  worker would edit files in place with no undo. Use `AskUserQuestion` to confirm before
  launching; if they decline, stop.

## 3. Launch

Spawn one background agent and return immediately:

```
Agent(
  subagent_type: "ouro-worker",
  run_in_background: true,
  isolation: "worktree",            // omit if not a git repo
  description: "ouro: <plan slug>",
  prompt: """
    Execute this plan autonomously under the ouro protocol.

    PLAN:        <absolute path to the plan>
    GUIDE MODEL: claude-opus-4-8
    ORIGIN REPO: <absolute path to the user's checkout>

    Read the plan, write .ouro/config, and begin. Consult the guide at forks and at the
    mandatory review checkpoints. Escalate to main only under the five triggers in your
    contract. Report once when done.
  """
)
```

Do not pass `isolation` if the directory is not a git repo.

## 4. Report and get out of the way

Print a short block — plan, worktree/branch, guide model, and that you'll surface anything
the worker escalates — then stop. **Do not poll the agent, do not narrate its progress, do
not spawn anything else.** You are notified when it escalates or finishes.

When an escalation arrives, relay it to the user as-is (it is already structured for them);
add your own recommendation only if you have context the worker lacks. Send their answer
back with `SendMessage` to the worker by name — it resumes with full context.
