#!/bin/zsh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config/a-system.env"
OFFLINE=0
[[ "${1:-}" == "--offline" ]] && OFFLINE=1
FAILURES=0
WARNINGS=0

ok() { echo "[OK] $1"; }
fail() { echo "[FAIL] $1"; FAILURES=$((FAILURES + 1)); }
warn() { echo "[WARN] $1"; WARNINGS=$((WARNINGS + 1)); }

DETECTED_ARCH="${A_SYSTEM_TEST_ARCH:-$(uname -m)}"
if [[ "$DETECTED_ARCH" == "arm64" || "$DETECTED_ARCH" == "x86_64" ]]; then
  ok "支持的 Mac 架构：$DETECTED_ARCH"
else
  fail "不支持的架构：$DETECTED_ARCH"
fi
[[ "$A_SYSTEM_ARCH" == "$DETECTED_ARCH" ]] && ok "安装架构与当前架构一致" || fail "安装架构 $A_SYSTEM_ARCH 与当前 $DETECTED_ARCH 不一致"

[[ -x "$A_SYSTEM_PYTHON" ]] && ok "Python：$A_SYSTEM_PYTHON" || fail "Python 不可执行"
"$A_SYSTEM_PYTHON" -c 'import sys; assert sys.version_info >= (3,10); import openpyxl' >/dev/null 2>&1 \
  && ok "Python 版本与 openpyxl 正常" || fail "Python/openpyxl 不可用"

[[ -d "/Applications/Google Chrome.app" ]] && ok "Google Chrome 已安装" || fail "未找到 Google Chrome"
[[ -f "$A_SYSTEM_DB" ]] && ok "v3 数据库存在" || fail "v3 数据库不存在"

DB_CHECK="$($A_SYSTEM_PYTHON - "$A_SYSTEM_DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    print(f"{integrity}|{jobs}|{candidates}")
finally:
    conn.close()
PY
)"
IFS='|' read -r INTEGRITY JOBS CANDIDATES <<< "$DB_CHECK"
[[ "$INTEGRITY" == "ok" ]] && ok "SQLite integrity_check=ok" || fail "SQLite 完整性异常：$INTEGRITY"
echo "[INFO] jobs=$JOBS candidates=$CANDIDATES"

[[ -f "$A_SYSTEM_HTML" ]] && ok "A 系统 HTML 已生成" || fail "A 系统 HTML 未生成"
for tab in 总览 岗位看板 人选进度 人选列表; do
  rg -q ">$tab</button>" "$A_SYSTEM_HTML" 2>/dev/null && ok "主入口：$tab" || fail "缺少主入口：$tab"
done

for extension in liepin-reply-assistant-extension xsaas-candidate-assistant-extension; do
  MANIFEST="$A_SYSTEM_HOME/extensions/$extension/manifest.json"
  if [[ -f "$MANIFEST" ]]; then
    VERSION="$($A_SYSTEM_PYTHON -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$MANIFEST")"
    ok "$extension 版本 $VERSION"
  else
    fail "缺少扩展：$extension"
  fi
done

if rg -n "/Users/messi/Documents/Codex/2026-06-(18|26)" "$A_SYSTEM_ROOT/work" "$A_SYSTEM_ROOT/tools" "$A_SYSTEM_LIEPIN_ROOT/scripts" >/tmp/a-system-portable-hardcoded.txt 2>/dev/null; then
  fail "仍有原机器业务路径残留：$(head -n 1 /tmp/a-system-portable-hardcoded.txt)"
else
  ok "未发现原机器业务路径残留"
fi

if [[ "$OFFLINE" -eq 0 ]]; then
  curl -fsS "http://127.0.0.1:$A_SYSTEM_PORT/api/context" >/dev/null 2>&1 \
    && ok "本机 $A_SYSTEM_PORT 服务正常" || fail "本机 $A_SYSTEM_PORT 服务未连接"
else
  warn "离线模式未检查 $A_SYSTEM_PORT 服务"
fi

if [[ "$JOBS" -gt 0 ]]; then
  "$A_SYSTEM_PYTHON" "$A_SYSTEM_ROOT/work/sync_a_system_client.py" --db "$A_SYSTEM_DB" --html "$A_SYSTEM_HTML" audit-all --strict >/tmp/a-system-portable-audit.json 2>&1 \
    && ok "严格全库审计通过" || fail "严格全库审计失败，见 /tmp/a-system-portable-audit.json"
  "$A_SYSTEM_PYTHON" "$A_SYSTEM_ROOT/tools/a_system_regression_guard.py" --db "$A_SYSTEM_DB" --html "$A_SYSTEM_HTML" --skip-audit >/tmp/a-system-portable-regression.json 2>&1 \
    && ok "A 系统回归检查通过" || fail "A 系统回归检查失败，见 /tmp/a-system-portable-regression.json"
else
  warn "当前为空库，跳过业务数据审计与 P0 回归"
fi

echo
echo "诊断完成：failures=$FAILURES warnings=$WARNINGS"
[[ "$FAILURES" -eq 0 ]]

