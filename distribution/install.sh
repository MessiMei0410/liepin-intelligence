#!/usr/bin/env bash
# ASA 安装器（路 B：同事自部署）——干净 Mac 从分发包装出完整 ASA。
#
# 用法：
#   ./install.sh [安装目录]        默认 ~/ASA
#
# 环境变量（可选）：
#   ASA_DEEPSEEK_KEY=sk-xxx   非交互提供 DeepSeek API Key（跳过提问，写入 Keychain + ~/.dsh/.credentials.yaml）
#   ASA_SKIP_KEY=1            跳过 DeepSeek Key 引导（后续可重跑本脚本补写）
#   ASA_SKIP_DSH=1            跳过 DSH 编排层安装（仅装 Core；对话大脑不可用）
#   ASA_SKIP_LAUNCHD=1        不注册 launchd 服务（渲染好的 plist 放在 安装目录/launchd/ 供检查）
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
ASA_HOME="${1:-${ASA_HOME:-$HOME/ASA}}"
ASA_HOME="${ASA_HOME/#\~/$HOME}"
CORE_PORT=8765
DSH_PORT=8891
KEYCHAIN_SERVICE="a-system-agent-deepseek"
KEYCHAIN_ACCOUNT="api.deepseek.com"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m安装中止：%s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$PKG_DIR/app" ] || die "分发包不完整：缺少 app/（请在分发包根目录运行本脚本）"
[ -f "$PKG_DIR/web/dist/index.html" ] || die "分发包不完整：缺少 web/dist/index.html"
[ -f "$PKG_DIR/app/base_schema.sql" ] || die "分发包不完整：缺少 app/base_schema.sql"

# ---------- 1. 系统依赖检查 ----------
step "1/9 检查系统依赖"
[ "$(uname -s)" = "Darwin" ] || die "ASA 目前只支持 macOS"

PY_BIN=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo 0.0)"
    major="${ver%%.*}"; minor="${ver##*.}"
    if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 11 ]; then
      PY_BIN="$(command -v "$cand")"
      break
    fi
  fi
done
if [ -z "$PY_BIN" ]; then
  die "未找到 Python 3.11+。请先安装：\n  brew install python@3.11\n（没装 Homebrew 的话先去 https://brew.sh 装一下）"
fi
ok "Python: $PY_BIN ($("$PY_BIN" --version 2>&1))"

if [ "${ASA_SKIP_DSH:-0}" != "1" ]; then
  command -v node >/dev/null 2>&1 || die "未找到 Node.js。请先安装：\n  brew install node"
  command -v npm  >/dev/null 2>&1 || die "未找到 npm（通常随 Node.js 一起安装：brew install node）"
  ok "Node.js: $(node --version)"
fi

if [ -d "/Applications/Google Chrome.app" ]; then
  ok "Chrome 已安装"
else
  warn "未检测到 Chrome——两个浏览器扩展（猎聘回复助手 / X-SaaS 候选人助手）需要 Chrome，装完 ASA 后请安装 Chrome 再加载扩展"
fi

# ---------- 2. 拷贝程序文件 ----------
step "2/9 安装到 $ASA_HOME"
mkdir -p "$ASA_HOME" "$ASA_HOME/data" "$ASA_HOME/logs"
rsync -a "$PKG_DIR/app/"        "$ASA_HOME/app/"
rsync -a "$PKG_DIR/web/"        "$ASA_HOME/web/"
rsync -a "$PKG_DIR/extensions/" "$ASA_HOME/extensions/"
rsync -a "$PKG_DIR/launchd/"    "$ASA_HOME/launchd/"
[ -d "$PKG_DIR/dsh" ] && rsync -a --exclude node_modules "$PKG_DIR/dsh/" "$ASA_HOME/dsh/"
cp "$PKG_DIR/INVENTORY.md" "$ASA_HOME/" 2>/dev/null || true
ok "程序文件已就位"

# ---------- 3. Python venv + 依赖 ----------
step "3/9 创建 Python 虚拟环境并安装依赖"
if [ ! -x "$ASA_HOME/venv/bin/python3" ]; then
  "$PY_BIN" -m venv "$ASA_HOME/venv" || die "venv 创建失败（python3 -m venv $ASA_HOME/venv）"
fi
"$ASA_HOME/venv/bin/pip" install --quiet --upgrade pip || warn "pip 自升级失败（不影响继续）"
if ! "$ASA_HOME/venv/bin/pip" install --quiet --timeout 60 --retries 3 fastapi 'uvicorn[standard]' pydantic; then
  warn "默认 PyPI 源安装失败，改用清华镜像重试…"
  "$ASA_HOME/venv/bin/pip" install --quiet --timeout 60 --retries 3 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple fastapi 'uvicorn[standard]' pydantic \
    || die "pip 安装 fastapi/uvicorn/pydantic 失败（默认源与清华镜像均不通）——请检查网络/代理后重跑本脚本"
fi
ok "依赖安装完成（fastapi / uvicorn / pydantic）"

# ---------- 4. 空库初始化 ----------
step "4/9 初始化空数据库"
A_SYSTEM_DB="$ASA_HOME/data/asa.db" "$ASA_HOME/venv/bin/python3" - "$ASA_HOME" <<'PYEOF' \
  || die "数据库初始化失败——把上方报错截图发给分发者"
import os, sqlite3, sys
from pathlib import Path
home = Path(sys.argv[1])
sys.path.insert(0, str(home / "app"))
db = home / "data" / "asa.db"
fresh = not db.exists()
conn = sqlite3.connect(str(db))
try:
    if fresh:
        conn.executescript((home / "app" / "base_schema.sql").read_text(encoding="utf-8"))
    from a_system_agent.schema import ensure_schema
    ensure_schema(conn)
    conn.commit()
finally:
    conn.close()
from asa_core.database import migrate
result = migrate(db)
print(f"    数据库: {db}（{'新建' if fresh else '已有，增量迁移'}）")
print(f"    迁移结果: ok={result['ok']} applied={result['applied']}")
if result["foreign_key_issues"]:
    print(f"    外键告警: {result['foreign_key_issues'][:3]}")
PYEOF
ok "空库 schema 就绪（v3 基座 + agent + asa_core 迁移）"

# ---------- 5. DeepSeek API Key ----------
step "5/9 配置 DeepSeek API Key"
DSH_DIR="$HOME/.dsh"
mkdir -p "$DSH_DIR"
CRED_FILE="$DSH_DIR/.credentials.yaml"

write_credentials() {  # $1 = key
  touch "$CRED_FILE"
  if grep -q '^DEEPSEEK_API_KEY:' "$CRED_FILE" 2>/dev/null; then
    sed -i '' "s|^DEEPSEEK_API_KEY:.*|DEEPSEEK_API_KEY: $1|" "$CRED_FILE"
  else
    printf 'DEEPSEEK_API_KEY: %s\n' "$1" >> "$CRED_FILE"
  fi
  chmod 600 "$CRED_FILE"
}

DEEPSEEK_KEY="${ASA_DEEPSEEK_KEY:-}"
if [ -z "$DEEPSEEK_KEY" ] && [ "${ASA_SKIP_KEY:-0}" != "1" ]; then
  if /usr/bin/security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w >/dev/null 2>&1; then
    ok "Keychain 已有 DeepSeek Key（${KEYCHAIN_SERVICE}），跳过"
  elif [ -t 0 ]; then
    printf '    请输入你的 DeepSeek API Key（sk- 开头，输入不回显；直接回车跳过）: '
    read -rs DEEPSEEK_KEY || true
    printf '\n'
  else
    warn "非交互环境且未提供 ASA_DEEPSEEK_KEY——跳过 Key 配置"
  fi
fi
if [ -n "$DEEPSEEK_KEY" ]; then
  case "$DEEPSEEK_KEY" in
    sk-*) ;;
    *) warn "Key 不像 sk- 开头，仍按原样写入（如不能用请重跑本脚本）" ;;
  esac
  /usr/bin/security add-generic-password -U -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w "$DEEPSEEK_KEY" \
    || die "写入 macOS Keychain 失败"
  write_credentials "$DEEPSEEK_KEY"
  ok "Key 已写入 Keychain（${KEYCHAIN_SERVICE}）+ ${CRED_FILE}（0600）"
elif [ "${ASA_SKIP_KEY:-0}" = "1" ]; then
  warn "按 ASA_SKIP_KEY=1 跳过——对话功能在写入 Key 前不可用；补写：ASA_DEEPSEEK_KEY=sk-xxx 重跑本脚本"
fi

# ---------- 6. DSH 共享密钥 ----------
step "6/9 生成 Core↔DSH 桥接密钥"
TOKEN_FILE="$DSH_DIR/asa-bridge-token"
if [ ! -s "$TOKEN_FILE" ]; then
  openssl rand -hex 32 > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  ok "已生成 ${TOKEN_FILE}（0600）"
else
  chmod 600 "$TOKEN_FILE"
  ok "已有 ${TOKEN_FILE}，保持不变"
fi

# ---------- 7. DSH 编排层 ----------
if [ "${ASA_SKIP_DSH:-0}" != "1" ]; then
  step "7/9 安装 DSH 常驻服务器（127.0.0.1:${DSH_PORT}）"
  [ -d "$ASA_HOME/dsh" ] || die "分发包缺少 dsh/ 目录"

  TOOLCHAIN="$DSH_DIR/asa-server-toolchain"
  mkdir -p "$TOOLCHAIN"
  cp "$ASA_HOME/dsh/package.json" "$ASA_HOME/dsh/package-lock.json" "$TOOLCHAIN/" 2>/dev/null \
    || cp "$ASA_HOME/dsh/package.json" "$TOOLCHAIN/"
  if ! diff -q "$ASA_HOME/dsh/package.json" "$TOOLCHAIN/.installed-package.json" >/dev/null 2>&1; then
    echo "    npm install DSH 工具链（首次约 1-3 分钟）…"
    npm_install_toolchain() {
      (cd "$TOOLCHAIN" && npm ci --no-fund --no-audit) || (cd "$TOOLCHAIN" && npm install --no-fund --no-audit)
    }
    if ! npm_install_toolchain; then
      warn "默认 npm 源失败，改用 npmmirror 镜像重试…"
      (cd "$TOOLCHAIN" && npm install --no-fund --no-audit --registry=https://registry.npmmirror.com) \
        || die "DSH 工具链 npm install 失败（默认源与 npmmirror 均不通）——检查网络/代理后重跑"
    fi
    cp "$ASA_HOME/dsh/package.json" "$TOOLCHAIN/.installed-package.json"
  fi
  [ -x "$TOOLCHAIN/node_modules/.bin/dsh" ] || die "DSH 工具链安装异常：$TOOLCHAIN/node_modules/.bin/dsh 不存在"
  ok "工具链就绪：$TOOLCHAIN"

  PROFILE="$DSH_DIR/profiles/asa-server"
  mkdir -p "$PROFILE/node_modules/@asa" "$DSH_DIR/asa-workspace"
  for f in package.json cordis.patch.yml AGENTS.md pnpm-workspace.yaml; do
    cp "$ASA_HOME/dsh/asa-server-profile/$f" "$PROFILE/$f"
  done
  rsync -a --delete --exclude node_modules "$ASA_HOME/dsh/asa-server/" "$PROFILE/node_modules/@asa/dsh-asa-server/"
  rsync -a --delete --exclude node_modules "$ASA_HOME/dsh/asa-tools/"   "$PROFILE/node_modules/@asa/dsh-asa-tools/"
  cp "$ASA_HOME/dsh/asa-server-profile/AGENTS.md" "$DSH_DIR/asa-workspace/AGENTS.md"
  ok "profile 与业务护栏已同步到 $PROFILE"

  if [ ! -f "$DSH_DIR/settings.yaml" ]; then
    cat > "$DSH_DIR/settings.yaml" <<'YAMLEOF'
agent-default-model:
  provider: deepseek-official
  model: deepseek-v4-flash
permission:
  defaultPreset: danger-full-access
YAMLEOF
    ok "已写入默认模型配置（deepseek-official / deepseek-v4-flash）"
  else
    ok "已有 settings.yaml，保持不变"
  fi
else
  step "7/9 跳过 DSH（ASA_SKIP_DSH=1）"
fi

# ---------- 8. launchd 服务 ----------
step "8/9 注册 launchd 常驻服务"
render_plist() {  # $1 模板 $2 输出
  sed -e "s|__ASA_HOME__|$ASA_HOME|g" -e "s|__HOME__|$HOME|g" "$1" > "$2"
}
LA_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LA_DIR"
if [ "${ASA_SKIP_LAUNCHD:-0}" = "1" ]; then
  render_plist "$PKG_DIR/launchd/ai.hermes.liepin-workbench.plist.template" "$ASA_HOME/launchd/ai.hermes.liepin-workbench.plist"
  render_plist "$PKG_DIR/launchd/com.asa.dsh-server.plist.template"        "$ASA_HOME/launchd/com.asa.dsh-server.plist"
  warn "按 ASA_SKIP_LAUNCHD=1 未注册服务；渲染好的 plist 在 $ASA_HOME/launchd/"
else
  render_plist "$PKG_DIR/launchd/ai.hermes.liepin-workbench.plist.template" "$LA_DIR/ai.hermes.liepin-workbench.plist"
  launchctl bootout "gui/$(id -u)/ai.hermes.liepin-workbench" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA_DIR/ai.hermes.liepin-workbench.plist" \
    || die "Core 服务注册失败：launchctl bootstrap gui/$(id -u) $LA_DIR/ai.hermes.liepin-workbench.plist"
  ok "Core 服务已注册（ai.hermes.liepin-workbench）"
  if [ "${ASA_SKIP_DSH:-0}" != "1" ]; then
    render_plist "$PKG_DIR/launchd/com.asa.dsh-server.plist.template" "$LA_DIR/com.asa.dsh-server.plist"
    launchctl bootout "gui/$(id -u)/com.asa.dsh-server" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$LA_DIR/com.asa.dsh-server.plist" \
      || die "DSH 服务注册失败：launchctl bootstrap gui/$(id -u) $LA_DIR/com.asa.dsh-server.plist"
    ok "DSH 服务已注册（com.asa.dsh-server）"
  fi
fi

# ---------- 9. 健康检查 ----------
step "9/9 健康检查"
wait_url() {  # $1 url, $2 期望特征, $3 名称, $4 额外 curl 参数
  local i
  for i in $(seq 1 20); do
    if curl -s --max-time 2 ${4:-} "$1" 2>/dev/null | grep -q "$2"; then
      ok "$3 正常（$1）"
      return 0
    fi
    sleep 1
  done
  return 1
}

CORE_UP=0
if [ "${ASA_SKIP_LAUNCHD:-0}" != "1" ]; then
  if wait_url "http://127.0.0.1:$CORE_PORT/api/v1/health" '"ok": *true\|"ok":true' "Core API"; then
    CORE_UP=1
    wait_url "http://127.0.0.1:$CORE_PORT/asa-app" '<html\|<div id="root"' "ASA Web 页面" "-A ASAApp/1.0" \
      || warn "/asa-app 未通过——看日志 $ASA_HOME/logs/asa-core.err.log"
  else
    warn "Core 未通过健康检查——看日志 $ASA_HOME/logs/asa-core.err.log"
  fi
  if [ "${ASA_SKIP_DSH:-0}" != "1" ]; then
    wait_url "http://127.0.0.1:$DSH_PORT/health" 'ok\|status' "DSH 常驻服务器" \
      || warn "DSH 未通过健康检查——看日志 $DSH_DIR/asa-server.err.log"
  fi
else
  echo "    （跳过 launchd，未起服务；手动起 Core：）"
  echo "    cd $ASA_HOME/app && A_SYSTEM_DB=$ASA_HOME/data/asa.db \\"
  echo "      $ASA_HOME/venv/bin/python3 -m asa_core.app --host 127.0.0.1 --port $CORE_PORT"
fi

# ---------- 完成 ----------
printf '\n\033[1;32m安装完成！\033[0m 安装目录：%s\n' "$ASA_HOME"
cat <<EOF

下一步（手动）：
  1. 装两个 Chrome 扩展：Chrome 打开 chrome://extensions → 右上角开「开发者模式」
     →「加载已解压的扩展程序」分别选择：
       $ASA_HOME/extensions/liepin-reply-assistant-extension
       $ASA_HOME/extensions/xsaas-candidate-assistant-extension
  2. ASA 桌面 App（ASA.app）打包在下一阶段提供；当前可用浏览器临时访问（需带 UA）：
       curl -A 'ASAApp/1.0' http://127.0.0.1:$CORE_PORT/asa-app
  3. 登录猎聘：扩展装好后打开 liepin.com 登录一次，登录态留在你自己的 Chrome 里。

常用维护命令：
  重启 Core:  launchctl kickstart -k gui/\$(id -u)/ai.hermes.liepin-workbench
  重启 DSH:   launchctl kickstart -k gui/\$(id -u)/com.asa.dsh-server
  Core 日志:  $ASA_HOME/logs/asa-core.err.log
  DSH 日志:   $DSH_DIR/asa-server.err.log
  重新安装/升级: 重跑本 install.sh（幂等：库只增量迁移，Key/密钥不覆盖）
EOF
