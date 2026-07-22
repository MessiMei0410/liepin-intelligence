#!/bin/zsh
cd "$(dirname "$0")"

PORT=8765
URL="http://127.0.0.1:${PORT}/workbench"
LOG="outputs/猎聘智能工作台服务.log"

if curl -fsS "http://127.0.0.1:${PORT}/api/context" >/dev/null 2>&1; then
  echo "猎聘智能工作台服务已在运行。"
else
  echo "正在启动猎聘智能工作台服务..."
  mkdir -p outputs
  nohup python3 scripts/liepin_workbench_server.py --port "${PORT}" > "${LOG}" 2>&1 &
  for i in {1..30}; do
    if curl -fsS "http://127.0.0.1:${PORT}/api/context" >/dev/null 2>&1; then
      break
    fi
    sleep 0.3
  done
fi

echo "正在打开工作台..."
open "${URL}"

echo "已打开猎聘智能工作台。这个窗口可以关闭，服务会在后台继续运行。"
