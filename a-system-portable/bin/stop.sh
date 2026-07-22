#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config/a-system.env"
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/ai.a-system.workbench" >/dev/null 2>&1 || true
PID="$(lsof -tiTCP:"$A_SYSTEM_PORT" -sTCP:LISTEN | head -n 1 || true)"
[[ -n "$PID" ]] && kill "$PID" || true
echo "A 系统服务已停止。"

