#!/usr/bin/env bash
# 部署 asa-server 常驻服务器：安装/更新工具链 → 同步 bundle 源码到 profile → 重启 → 健康检查。
#
# 背景：
# - profile（~/.dsh/profiles/asa-server）的 file: 安装是「拷贝」，改完
#   dsh/asa-server 或 dsh/asa-tools 必须重新同步才会生效；常驻服务器本身无热重载。
# - 运行用工具链固定在 ~/.dsh/asa-server-toolchain（版本随 dsh/package.json 锁定）。
#   不能直接用仓库里的 node_modules：launchd 拉起的进程没有 ~/Documents 的 TCC 授权，
#   读仓库路径会 EPERM。
#
# 重启策略：已装 launchd（com.asa.dsh-server，见 dsh/launchd/）则 kickstart，
# 否则退回 nohup 前台兜底（无崩溃守护，仅建议 dev 用）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLCHAIN_DIR="${ASA_DSH_TOOLCHAIN_DIR:-$HOME/.dsh/asa-server-toolchain}"
PROFILE_DIR="${ASA_DSH_PROFILE_DIR:-$HOME/.dsh/profiles/asa-server}"
# 常驻服务器工作目录：须与 launchd plist 的 WorkingDirectory 一致；放 ~/.dsh 是因为
# launchd 进程没有 ~/Documents 的 TCC 授权，而 /tmp 会被系统清理。
WORKDIR="${ASA_DSH_WORKDIR:-$HOME/.dsh/asa-workspace}"
LABEL="com.asa.dsh-server"
PORT="${ASA_DSH_RESIDENT_PORT:-8891}"

# 1. 安装/更新工具链（dsh/package.json 锁定版本；内容变化才重装）
if [[ ! -x "$TOOLCHAIN_DIR/node_modules/.bin/dsh" ]] || ! diff -q "$REPO_ROOT/package.json" "$TOOLCHAIN_DIR/package.json" >/dev/null 2>&1; then
  echo "[deploy] installing dsh toolchain -> $TOOLCHAIN_DIR"
  mkdir -p "$TOOLCHAIN_DIR"
  cp "$REPO_ROOT/package.json" "$TOOLCHAIN_DIR/package.json"
  (cd "$TOOLCHAIN_DIR" && npm install --no-fund --no-audit)
fi

# 2. 同步 bundle 源码到 profile 拷贝
for pkg in asa-server asa-tools; do
  target="$PROFILE_DIR/node_modules/@asa/dsh-$pkg"
  if [[ ! -d "$target" ]]; then
    echo "[deploy] $target 不存在——profile 未安装？按 dsh/README.md「快速开始」先装 profile。" >&2
    exit 1
  fi
  rsync -a --delete --exclude node_modules "$REPO_ROOT/$pkg/" "$target/"
  echo "[deploy] synced $pkg -> $target"
done

# 2b. 同步 profile patch 与工作目录护栏（这两个文件不在 bundle 里，仓库即事实源）
cp "$REPO_ROOT/asa-server-profile/cordis.patch.yml" "$PROFILE_DIR/cordis.patch.yml"
mkdir -p "$WORKDIR"
cp "$REPO_ROOT/asa-server-profile/AGENTS.md" "$WORKDIR/AGENTS.md"
echo "[deploy] synced cordis.patch.yml -> profile, AGENTS.md -> $WORKDIR"

# 3. 重启常驻服务器
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "[deploy] launchctl kickstart $LABEL"
else
  echo "[deploy] launchd $LABEL 未安装，退回 nohup（无崩溃守护；建议安装 dsh/launchd/ 下的 plist）"
  pkill -f "profile asa-server" 2>/dev/null || true
  sleep 1
  cd "$WORKDIR"
  nohup "$TOOLCHAIN_DIR/node_modules/.bin/dsh" --profile asa-server > /tmp/asa-server.log 2>&1 &
fi

# 4. 健康检查
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "[deploy] asa-server healthy on $PORT"
    exit 0
  fi
  sleep 1
done
echo "[deploy] asa-server 健康检查失败，查 /tmp/asa-server.log 或 ~/.dsh/asa-server.err.log" >&2
exit 1
