#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PURGE=0
[[ "${1:-}" == "--purge-data" ]] && PURGE=1
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/ai.a-system.workbench" >/dev/null 2>&1 || true
launchctl bootout "$DOMAIN/ai.a-system.chrome-cdp" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/ai.a-system.workbench.plist"
rm -f "$HOME/Library/LaunchAgents/ai.a-system.chrome-cdp.plist"

if [[ "$PURGE" -eq 0 && -d "$ROOT/app/outputs" ]]; then
  BACKUP="$HOME/A-System-data-backup-$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$BACKUP"
  cp -R "$ROOT/app/outputs" "$BACKUP/"
  echo "数据已保留到：$BACKUP"
fi

rm -rf "$ROOT"
echo "A 系统已卸载。"

