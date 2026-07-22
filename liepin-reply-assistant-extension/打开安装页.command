#!/usr/bin/env bash
set -euo pipefail

EXT_DIR="/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/liepin-reply-assistant-extension"

open -a "Google Chrome" "chrome://extensions/"
open "$EXT_DIR"

printf '已打开 Chrome 扩展页和插件文件夹。\n'
printf '在 Chrome 扩展页开启开发者模式后，选择“加载已解压的扩展程序”，再选择：%s\n' "$EXT_DIR"
