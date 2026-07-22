#!/bin/zsh
set -e

cd "$(dirname "$0")"

echo "记录客户反馈 / 更新候选人状态"
echo "用于记录客户认可、否决、面试、offer、入职等反馈。"
echo "选完后需要输入 y 才会写入。"
echo ""

python3 scripts/record_client_feedback.py --interactive

echo ""
echo "正在刷新猎聘智能数据..."
python3 scripts/refresh_liepin_intelligence.py

echo ""
echo "完成。按任意键关闭窗口。"
read -k 1
