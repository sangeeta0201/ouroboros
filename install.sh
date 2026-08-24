#!/usr/bin/env bash
# Install the /ouro command, worker agent, and guide driver into ~/.claude.
#
# Symlinks by default, so this repo stays the source of truth and edits here take
# effect immediately. Pass --copy to install detached copies instead.
#
#   ./install.sh            # symlink into ~/.claude
#   ./install.sh --copy     # copy instead of symlink
#   ./install.sh --uninstall

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MODE="symlink"

case "${1:-}" in
  --copy)      MODE="copy" ;;
  --uninstall) MODE="uninstall" ;;
  "")          ;;
  *) echo "usage: $0 [--copy|--uninstall]" >&2; exit 2 ;;
esac

# repo-relative path : destination path
FILES=(
  ".claude/commands/ouro.md:$DEST/commands/ouro.md"
  ".claude/agents/ouro-worker.md:$DEST/agents/ouro-worker.md"
  ".claude/ouro/guide.sh:$DEST/ouro/guide.sh"
  ".claude/ouro/guide-protocol.md:$DEST/ouro/guide-protocol.md"
)

if [[ "$MODE" == "uninstall" ]]; then
  for pair in "${FILES[@]}"; do
    dst="${pair#*:}"
    if [[ -L "$dst" || -f "$dst" ]]; then
      rm -f "$dst"
      echo "removed  $dst"
    fi
  done
  rmdir "$DEST/ouro" 2>/dev/null || true
  echo
  echo "Uninstalled. Restart Claude Code so the agent is de-registered."
  exit 0
fi

for pair in "${FILES[@]}"; do
  src="$REPO/${pair%%:*}"
  dst="${pair#*:}"

  [[ -f "$src" ]] || { echo "missing source: $src" >&2; exit 1; }
  mkdir -p "$(dirname "$dst")"

  # Never clobber a real file silently — back it up first. Our own symlink is
  # safe to replace.
  if [[ -e "$dst" && ! -L "$dst" ]]; then
    mv "$dst" "$dst.bak.$$"
    echo "backed up existing $dst -> $dst.bak.$$"
  fi
  rm -f "$dst"

  if [[ "$MODE" == "copy" ]]; then
    cp "$src" "$dst"
    echo "copied   $dst"
  else
    ln -s "$src" "$dst"
    echo "linked   $dst -> $src"
  fi
done

chmod +x "$DEST/ouro/guide.sh" 2>/dev/null || true

echo
echo "Installed. Two things before first use:"
echo "  1. Restart Claude Code — slash commands hot-reload, agents do not."
echo "  2. Check the guide model: OURO_GUIDE_MODEL (default claude-opus-4-8)."
echo "     It must differ from the model your worker runs on."
