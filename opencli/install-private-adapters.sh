#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
XSAAS_SOURCE="$ROOT/clis/xsaas/candidate-search.js"
XSAAS_TARGET_DIR="$HOME/.opencli/clis/xsaas"
XSAAS_TARGET="$XSAAS_TARGET_DIR/candidate-search.js"
LIEPIN_SOURCE="$ROOT/clis/liepin/candidate-search.js"
LIEPIN_TARGET_DIR="$HOME/.opencli/clis/liepin"
LIEPIN_TARGET="$LIEPIN_TARGET_DIR/candidate-search.js"
APPS_CONFIG="$HOME/.opencli/apps.yaml"
APPS_SOURCE="$ROOT/apps.yaml"
OPENCLI_BIN="${OPENCLI_BIN:-/Users/messi/.hermes/node/bin/opencli}"

mkdir -p "$XSAAS_TARGET_DIR" "$LIEPIN_TARGET_DIR"
install -m 0644 "$XSAAS_SOURCE" "$XSAAS_TARGET"
install -m 0644 "$LIEPIN_SOURCE" "$LIEPIN_TARGET"
if [[ ! -e "$APPS_CONFIG" ]]; then
  install -m 0600 "$APPS_SOURCE" "$APPS_CONFIG"
elif jq -e . "$APPS_CONFIG" >/dev/null 2>&1; then
  MERGED="$(mktemp)"
  jq -s '.[0] * .[1]' "$APPS_CONFIG" "$APPS_SOURCE" >"$MERGED"
  install -m 0600 "$MERGED" "$APPS_CONFIG"
  rm -f "$MERGED"
else
  printf 'Refusing to rewrite non-JSON YAML config %s; add liepin and xsaas app entries manually.\n' "$APPS_CONFIG" >&2
  exit 1
fi
"$OPENCLI_BIN" validate liepin/candidate-search
"$OPENCLI_BIN" validate xsaas/candidate-search

printf 'Installed %s\n' "$LIEPIN_TARGET"
printf 'Installed %s\n' "$XSAAS_TARGET"
