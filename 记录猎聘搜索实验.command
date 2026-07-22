#!/bin/zsh
set -e

cd "$(dirname "$0")"

echo "记录猎聘搜索实验"
echo "一轮搜索结束后，把关键词、筛选、查看人数、入库人数记下来。"
echo "后续有推荐或回复，可以再次运行并输入编号回填。"
echo ""

read "experiment_id?如果是回填已有搜索实验，输入编号；新增请直接回车："

if [[ -n "$experiment_id" ]]; then
  python3 scripts/record_search_experiment.py --interactive --experiment-id "$experiment_id"
else
  python3 scripts/record_search_experiment.py --interactive
fi

echo ""
echo "正在生成搜索复盘..."
python3 scripts/generate_search_experiment_report.py

echo ""
echo "完成。按任意键关闭窗口。"
read -k 1
