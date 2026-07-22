#!/bin/zsh
set -euo pipefail

PORT=8765
BASE_DIR="/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence"
SCRIPTS_DIR="$BASE_DIR/scripts"
LOG_FILE="/tmp/liepin_workbench_server.log"

echo "正在重启猎聘工作台服务..."

PID="$(lsof -tiTCP:${PORT} -sTCP:LISTEN | head -n 1 || true)"
if [[ -n "$PID" ]]; then
  echo "停止旧服务 PID=$PID"
  kill "$PID" || true
  sleep 1
fi

if lsof -tiTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  echo "旧服务仍占用 ${PORT}，尝试强制停止"
  PID="$(lsof -tiTCP:${PORT} -sTCP:LISTEN | head -n 1 || true)"
  [[ -n "$PID" ]] && kill -9 "$PID" || true
  sleep 1
fi

cd "$SCRIPTS_DIR"
echo "启动新服务：http://127.0.0.1:${PORT}"
nohup python3 liepin_workbench_server.py --host 127.0.0.1 --port "$PORT" --no-open-refresh > "$LOG_FILE" 2>&1 &
sleep 2

echo "检查接口..."
if curl -s --max-time 4 "http://127.0.0.1:${PORT}/api/context" >/dev/null; then
  echo "服务已运行：http://127.0.0.1:${PORT}/workbench"
else
  echo "服务启动后接口未响应，请查看日志：$LOG_FILE"
  tail -80 "$LOG_FILE" || true
fi

echo
echo "可以关闭这个窗口。"
