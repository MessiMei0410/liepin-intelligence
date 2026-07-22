#!/bin/zsh
set -euo pipefail

BASE_DIR="/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence"
LOG_FILE="/tmp/liepin_cdp_fix.log"

cd "$BASE_DIR"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 修复猎聘 CDP Chrome ====="
  /usr/bin/python3 scripts/ensure_liepin_cdp_chrome.py
  echo
  echo "结果文件：/tmp/liepin_cdp_status.json"
  echo "日志文件：$LOG_FILE"
  echo "可以关闭这个窗口。"
} 2>&1 | tee "$LOG_FILE"
