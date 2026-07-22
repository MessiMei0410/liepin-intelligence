#!/bin/zsh
set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="$HOME/A-System"
PORT=8765
CDP_PORT=9223
SKIP_PIP=0
NO_LAUNCHAGENT=0
NO_CDP=0
NO_START=0
SKIP_SKILL=0
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) INSTALL_ROOT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --cdp-port) CDP_PORT="$2"; shift 2 ;;
    --skip-pip) SKIP_PIP=1; shift ;;
    --no-launchagent) NO_LAUNCHAGENT=1; shift ;;
    --no-cdp) NO_CDP=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --skip-skill) SKIP_SKILL=1; shift ;;
    --codex-home) CODEX_HOME="$2"; shift 2 ;;
    -h|--help)
      echo "用法: ./install.sh [--root 路径] [--skip-pip] [--no-launchagent] [--no-cdp] [--no-start]"
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "A 系统便携版当前只支持 macOS。" >&2
  exit 1
fi

ARCH="${A_SYSTEM_TEST_ARCH:-$(uname -m)}"
if [[ "$ARCH" != "arm64" && "$ARCH" != "x86_64" ]]; then
  echo "不支持的架构: $ARCH" >&2
  exit 1
fi

BOOTSTRAP_PYTHON="${A_SYSTEM_PYTHON_BOOTSTRAP:-$(command -v python3 || true)}"
if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
  echo "未找到 Python 3。请先安装 Python 3.11。" >&2
  exit 1
fi

"$BOOTSTRAP_PYTHON" -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"'
mkdir -p "$INSTALL_ROOT"

if [[ "$SKIP_PIP" -eq 0 ]]; then
  "$BOOTSTRAP_PYTHON" -m venv "$INSTALL_ROOT/.venv"
  "$INSTALL_ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$INSTALL_ROOT/.venv/bin/python" -m pip install -r "$BUNDLE_ROOT/requirements.txt"
  RUNTIME_PYTHON="$INSTALL_ROOT/.venv/bin/python"
else
  RUNTIME_PYTHON="$BOOTSTRAP_PYTHON"
  "$RUNTIME_PYTHON" -c 'import openpyxl' || {
    echo "--skip-pip 模式需要当前 Python 已安装 openpyxl。" >&2
    exit 1
  }
fi

CONFIGURE_ARGS=(
  --bundle-root "$BUNDLE_ROOT"
  --install-root "$INSTALL_ROOT"
  --python-bin "$RUNTIME_PYTHON"
  --arch "$ARCH"
  --port "$PORT"
  --cdp-port "$CDP_PORT"
  --codex-home "$CODEX_HOME"
)
[[ "$NO_LAUNCHAGENT" -eq 1 ]] && CONFIGURE_ARGS+=(--no-launchagent)
[[ "$NO_CDP" -eq 1 ]] && CONFIGURE_ARGS+=(--no-cdp)
[[ "$SKIP_SKILL" -eq 1 ]] && CONFIGURE_ARGS+=(--skip-skill)

"$BOOTSTRAP_PYTHON" "$BUNDLE_ROOT/configure_install.py" "${CONFIGURE_ARGS[@]}"

source "$INSTALL_ROOT/config/a-system.env"
"$A_SYSTEM_PYTHON" "$A_SYSTEM_ROOT/work/build_talent_workbench.py"

if [[ "$NO_LAUNCHAGENT" -eq 0 && "$NO_START" -eq 0 ]]; then
  DOMAIN="gui/$(id -u)"
  launchctl bootout "$DOMAIN/ai.a-system.workbench" >/dev/null 2>&1 || true
  launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/ai.a-system.workbench.plist"
fi

if [[ "$NO_START" -eq 0 ]]; then
  "$INSTALL_ROOT/bin/start.sh"
fi

echo
echo "安装完成：$INSTALL_ROOT"
echo "架构：$ARCH"
echo "猎聘扩展：$INSTALL_ROOT/extensions/liepin-reply-assistant-extension"
echo "X-SaaS 扩展：$INSTALL_ROOT/extensions/xsaas-candidate-assistant-extension"
echo "诊断：$INSTALL_ROOT/bin/doctor.sh"
