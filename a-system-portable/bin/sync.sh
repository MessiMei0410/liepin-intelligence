#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config/a-system.env"
exec "$A_SYSTEM_PYTHON" "$A_SYSTEM_ROOT/tools/a_system_sync.py" --db "$A_SYSTEM_DB" --html "$A_SYSTEM_HTML" "$@"

