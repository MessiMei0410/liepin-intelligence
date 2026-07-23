#!/bin/bash
# 安装/重载 ASA v3 每日备份 LaunchAgent（PRD R13）。
# 用法: bash scripts/install_v3_backup_agent.sh
set -euo pipefail

LABEL="ai.hermes.asa-v3-backup"
SRC="/Users/messi/Documents/ASA/scripts/launchagents/${LABEL}.plist"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

cp "$SRC" "$DEST"
plutil -lint "$DEST" >/dev/null

# 已加载则先卸载再装载（幂等重装）。
if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "${DOMAIN}/${LABEL}"
fi
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl print "${DOMAIN}/${LABEL}" >/dev/null
echo "installed: ${LABEL}（每日 09:41 本地时间执行，日志 ~/.hermes/logs/asa_v3_backup*.log）"
