#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/messi/Documents/ASA/opencli"
CHROME="/Users/messi/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
PROFILE="$ROOT/chrome-profile"
EXTENSION="$ROOT/opencli-extension-v1.0.22"
LOG="/tmp/opencli_chrome_testing.log"

mkdir -p "$PROFILE"
pkill -f "$PROFILE" >/dev/null 2>&1 || true
/Users/messi/.hermes/node/bin/opencli daemon stop >/dev/null 2>&1 || true

nohup "$CHROME" \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port=9333 \
  --load-extension="$EXTENSION" \
  --disable-extensions-except="$EXTENSION" \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-timer-throttling \
  about:blank >"$LOG" 2>&1 &

sleep 8
/Users/messi/.hermes/node/bin/opencli doctor
