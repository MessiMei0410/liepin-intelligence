#!/bin/bash
# Launch Chrome with remote debugging for Hermes CDP automation
# Usage: bash chrome_cdp.sh
# Persists cookies/login in ~/.hermes/chrome_profile/
#
# After first login, cookies survive across restarts.
# Use this script every time you need Chrome for Liepin automation.

PROFILE_DIR="$HOME/.hermes/chrome_profile"
mkdir -p "$PROFILE_DIR"

# Kill any existing debug Chrome on the same port
pkill -f "chrome.*remote-debugging-port=9222" 2>/dev/null
sleep 1

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  &

echo "Chrome started with CDP on :9222"
echo "Profile: $PROFILE_DIR"
sleep 2

# Verify
curl -s http://127.0.0.1:9222/json/version | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'Browser: {d[\"Browser\"]}')" 2>/dev/null && echo "CDP OK" || echo "CDP FAIL - check if Chrome is running"
