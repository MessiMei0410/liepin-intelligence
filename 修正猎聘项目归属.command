#!/bin/zsh
set -e

cd "$(dirname "$0")"

echo "修正猎聘项目归属"
echo "用于把未定客户/未定岗位的回复和待办，挂到正确客户/岗位。"
echo "选完后需要输入 y 才会写入。"
echo ""

python3 scripts/confirm_project_assignment.py --interactive

echo ""
echo "正在刷新猎聘智能数据..."
python3 scripts/refresh_liepin_intelligence.py

echo ""
echo "完成。按任意键关闭窗口。"
read -k 1
