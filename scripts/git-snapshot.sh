#!/usr/bin/env bash
# ASA 项目 Git 快照脚本：一次性完成 status -> add -> commit -> push
# 用法：bash scripts/git-snapshot.sh "修复了 xxx"
set -euo pipefail

cd "$(dirname "$0")/.."

MSG="${1:-}"
if [ -z "$MSG" ]; then
    echo "用法：bash scripts/git-snapshot.sh \"这次改动的描述\"" >&2
    exit 1
fi

echo "=== 当前改动 ==="
git status --short

echo ""
echo "=== 加入暂存区 ==="
git add -A

echo ""
echo "=== 提交 ==="
git commit -m "$MSG"

echo ""
echo "=== 推送到远程 ==="
if git remote get-url origin >/dev/null 2>&1; then
    git push
else
    echo "没有配置远程仓库（origin），跳过 push。" >&2
fi

echo ""
echo "完成。最新提交："
git log --oneline -1
