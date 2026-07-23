#!/bin/bash
# ship.sh — 里程碑部署一键化：build → 重启 Core → 提示重开 ASA.app
# 用法：bash scripts/ship.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/2 构建前端 dist/"
npm run build

echo "==> 2/2 重启 ASA Core（launchd）"
launchctl kickstart -k "gui/$(id -u)/ai.hermes.liepin-workbench"

echo ""
echo "✅ 部署完成。请重开 ASA.app（或在 App 内刷新）以加载最新前端。"
