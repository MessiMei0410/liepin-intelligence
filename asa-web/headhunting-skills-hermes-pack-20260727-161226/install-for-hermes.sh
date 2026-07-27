#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/skills"
TARGET_DIR="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"
BACKUP_DIR="$HOME/.codex/skills_backup_$(date +%Y%m%d_%H%M%S)"

if [ ! -d "$SOURCE_DIR" ]; then
  echo "ERROR: skills directory not found: $SOURCE_DIR" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"
mkdir -p "$BACKUP_DIR"

installed_count=0
backed_up_count=0

for skill_path in "$SOURCE_DIR"/*; do
  [ -d "$skill_path" ] || continue
  skill_name="$(basename "$skill_path")"
  target_path="$TARGET_DIR/$skill_name"

  if [ -d "$target_path" ]; then
    mv "$target_path" "$BACKUP_DIR/$skill_name"
    backed_up_count=$((backed_up_count + 1))
  fi

  cp -R "$skill_path" "$target_path"
  installed_count=$((installed_count + 1))
  echo "installed: $skill_name"
done

echo
echo "Done."
echo "Installed skills: $installed_count"
echo "Backed up existing skills: $backed_up_count"
echo "Target: $TARGET_DIR"

if [ "$backed_up_count" -gt 0 ]; then
  echo "Backup: $BACKUP_DIR"
fi
