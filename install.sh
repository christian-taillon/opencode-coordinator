#!/bin/sh
# Copy the coordinator into the standard OpenCode config directory.
# Stops if any destination exists. Does not change models, permissions, or opencode.json.
set -eu
unset CDPATH

root=$(cd -- "$(dirname "$0")" && pwd)
dest=${OPENCODE_CONFIG_DIR:-"$HOME/.config/opencode"}

if [ "$dest" != "$HOME/.config/opencode" ]; then
  printf 'install: this bundle expects %s\n' "$HOME/.config/opencode" >&2
  printf 'install: its commands call the helper at that path\n' >&2
  exit 2
fi

for path in \
  "$dest/agents/opencode.md" \
  "$dest/skills/session-coordination" \
  "$dest/commands/session-review.md" \
  "$dest/commands/session-act.md"
do
  if [ -e "$path" ]; then
    printf 'install: refusing to replace %s\n' "$path" >&2
    printf 'install: compare, back up, and replace it explicitly\n' >&2
    exit 1
  fi
done

mkdir -p "$dest/agents" "$dest/commands" "$dest/skills/session-coordination/scripts"
install -m 644 "$root/agents/opencode.md" "$dest/agents/opencode.md"
install -m 644 "$root/skills/session-coordination/SKILL.md" \
  "$dest/skills/session-coordination/SKILL.md"
install -m 755 "$root/skills/session-coordination/scripts/sessions" \
  "$dest/skills/session-coordination/scripts/sessions"
install -m 644 "$root/commands/session-review.md" "$root/commands/session-act.md" \
  "$dest/commands/"

printf 'Installed the coordinator into %s\n' "$dest"
printf 'Choose an available model in agents/opencode.md before first use if needed.\n'
printf 'Start with: opencode run --agent opencode "What is running, and what needs me?"\n'
