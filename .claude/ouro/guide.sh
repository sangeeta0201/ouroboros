#!/usr/bin/env bash
# ouro-guide — consult the guide model (a second, read-only Claude session).
#
# Usage:
#   ouro-guide ask    "<question>"
#   ouro-guide review "<what you built and what should be checked>"
#
# Exit codes (the worker MUST branch on these):
#   0   ruling delivered (ask), or review returned OK
#   10  ruling is ESCALATE  -> stop and escalate to main
#   11  review returned FIX -> fix the listed problems, then review again
#   3   guide unreachable after retry -> escalate to main, do not guess
#   2   usage error
#
# Config: reads .ouro/config in the worktree if present. Environment wins.
#   OURO_PLAN         absolute path to the plan being executed
#   OURO_GUIDE_MODEL  model for the guide (default: claude-opus-4-8)

set -uo pipefail

MODE="${1:-}"
shift || true
QUESTION="${*:-}"

if [[ "$MODE" != "ask" && "$MODE" != "review" ]] || [[ -z "${QUESTION// }" ]]; then
  echo "usage: ouro-guide {ask|review} \"<text>\"" >&2
  exit 2
fi

OURO_DIR="${OURO_DIR:-$PWD/.ouro}"
mkdir -p "$OURO_DIR"
[[ -f "$OURO_DIR/config" ]] && . "$OURO_DIR/config"

: "${OURO_GUIDE_MODEL:=claude-opus-4-8}"
: "${OURO_PLAN:=}"
: "${OURO_GUIDE_PROTOCOL:=$HOME/.claude/ouro/guide-protocol.md}"

SESSION_FILE="$OURO_DIR/guide-session"

if [[ ! -f "$OURO_GUIDE_PROTOCOL" ]]; then
  echo "ouro-guide: protocol file missing: $OURO_GUIDE_PROTOCOL" >&2
  exit 3
fi

SYSTEM="$(cat "$OURO_GUIDE_PROTOCOL")"
if [[ -n "$OURO_PLAN" ]]; then
  SYSTEM+=$'\n\n## The plan being executed\n\n'"$OURO_PLAN"$'\n\nRead it before ruling. It is the authoritative statement of intent.'
else
  SYSTEM+=$'\n\n## The plan being executed\n\n(none supplied — ground rulings in CLAUDE.md, docs, and the code)'
fi
SYSTEM+=$'\n\nWorking directory: '"$PWD"

if [[ "$MODE" == "ask" ]]; then
  PROMPT="The worker is asking:

$QUESTION

Rule on it. If the answer is not determinable from the plan, docs, or code, reply ESCALATE: <what the human must decide, and why>."
else
  PROMPT="REVIEW REQUEST. The worker reports:

$QUESTION

Independently verify this against the plan and the actual state of the code — do not take
the worker's summary at face value. Reply OK: <what you verified> or FIX: <specific problems>."
fi

run_guide() {
  local args=(
    -p
    --model "$OURO_GUIDE_MODEL"
    --tools "Read,Grep,Glob"
    --permission-mode bypassPermissions
    --output-format json
    --append-system-prompt "$SYSTEM"
  )
  if [[ -s "$SESSION_FILE" ]]; then
    args+=( --resume "$(cat "$SESSION_FILE")" )
  fi
  claude "${args[@]}" "$PROMPT" 2>>"$OURO_DIR/guide-stderr.log"
}

RAW="$(run_guide)"
if [[ -z "${RAW// }" ]]; then
  sleep 3
  # A stale/unresumable session id is the most likely cause; retry fresh.
  rm -f "$SESSION_FILE"
  RAW="$(run_guide)"
fi

if [[ -z "${RAW// }" ]]; then
  echo "ouro-guide: no response from guide after retry (see $OURO_DIR/guide-stderr.log)" >&2
  exit 3
fi

PARSED="$(printf '%s' "$RAW" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("session_id", ""))
print((d.get("result") or "").strip())
' 2>/dev/null)" || {
  echo "ouro-guide: could not parse guide response" >&2
  printf '%s\n' "$RAW" >>"$OURO_DIR/guide-stderr.log"
  exit 3
}

SESSION_ID="$(printf '%s' "$PARSED" | head -n 1)"
RULING="$(printf '%s' "$PARSED" | tail -n +2)"

[[ -n "$SESSION_ID" ]] && printf '%s' "$SESSION_ID" >"$SESSION_FILE"

if [[ -z "${RULING// }" ]]; then
  echo "ouro-guide: guide returned an empty ruling" >&2
  exit 3
fi

# Confidence score — only meaningful on reviews. Recorded so the human can see, at a
# glance, how much of the worker's report the repo actually corroborated.
CONF=""
if [[ "$MODE" == "review" ]]; then
  CONF="$(printf '%s\n' "$RULING" \
    | sed -n 's/^[[:space:]]*CONFIDENCE:[[:space:]]*\([0-9]\{1,3\}\).*/\1/p' | head -n 1)"
  if [[ -n "$CONF" ]]; then
    printf '%s' "$CONF" >"$OURO_DIR/confidence"
  else
    echo "ouro-guide: WARNING — review reply carried no CONFIDENCE line" >&2
  fi
fi

# Transcript — proof the guide was actually consulted, and what it said.
N="$(printf '%02d' "$(( $(ls "$OURO_DIR"/guide-*.md 2>/dev/null | wc -l | tr -d ' ') + 1 ))")"
{
  printf '# guide %s (%s)%s\n\n## Asked\n\n%s\n\n## Ruling\n\n%s\n' \
    "$N" "$MODE" "${CONF:+ — confidence $CONF/100}" "$QUESTION" "$RULING"
} >"$OURO_DIR/guide-$N.md"

printf '%s\n' "$RULING"

case "$RULING" in
  ESCALATE:*) exit 10 ;;
  FIX:*)      exit 11 ;;
esac
exit 0
