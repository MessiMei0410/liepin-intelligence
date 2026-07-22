#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config/a-system.env"

"$A_SYSTEM_PYTHON" "$A_SYSTEM_ROOT/work/build_talent_workbench.py"

if [[ -f "$HOME/Library/LaunchAgents/ai.a-system.workbench.plist" ]]; then
  DOMAIN="gui/$(id -u)"
  if ! launchctl print "$DOMAIN/ai.a-system.workbench" >/dev/null 2>&1; then
    launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/ai.a-system.workbench.plist"
  fi
  launchctl kickstart -k "$DOMAIN/ai.a-system.workbench"
else
  if ! curl -fsS "http://127.0.0.1:$A_SYSTEM_PORT/api/context" >/dev/null 2>&1; then
    nohup "$A_SYSTEM_PYTHON" "$A_SYSTEM_LIEPIN_ROOT/scripts/liepin_workbench_server.py" \
      --host 127.0.0.1 --port "$A_SYSTEM_PORT" --no-open-refresh \
      > "$A_SYSTEM_HOME/logs/workbench.log" 2> "$A_SYSTEM_HOME/logs/workbench-error.log" &
  fi
fi

for _ in {1..40}; do
  curl -fsS "http://127.0.0.1:$A_SYSTEM_PORT/api/context" >/dev/null 2>&1 && break
  sleep 0.25
done

curl -fsS "http://127.0.0.1:$A_SYSTEM_PORT/api/context" >/dev/null
open -a "Google Chrome" "$A_SYSTEM_HTML"
echo "A 系统已启动：http://127.0.0.1:$A_SYSTEM_PORT/workbench"

