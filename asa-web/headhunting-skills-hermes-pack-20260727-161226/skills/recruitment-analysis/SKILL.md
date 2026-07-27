---
name: recruitment-analysis
description: Analyze semiconductor equipment recruitment (JD) tables — clean OCR noise, structure data, generate formatted Excel + Word analysis report. Trigger when user shares a recruitment/headcount planning table with garbled text.
---

# Recruitment Table Analysis (招聘需求分析)

End-to-end pipeline: raw OCR'd Excel → cleaned data → formatted Excel → Word analysis report.

## Trigger Conditions

- User shares an Excel file with recruitment / headcount / JD data
- Text is garbled (OCR noise, misaligned columns)
- User asks to "analyze positions", "整理招聘需求", "分析岗位"

## Pipeline

### Step 1: Extract Raw Data

```bash
python3 -c "
import openpyxl
wb = openpyxl.load_workbook('FILE_PATH')
for name in wb.sheetnames:
    ws = wb[name]
    for row in ws.iter_rows(values_only=True):
        print([str(c) if c is not None else '' for c in row])
"
```

### Step 2: Clean OCR Text

Common OCR error patterns in Chinese semiconductor context:

| OCR Output | Correct |
|------------|---------|
| 负声/负声 | 负责 |
| 肝发/码发 | 研发 |
| 路历/路压/路思 | 熟悉/熟练 |
| 岁真/访喜/达嘉 | 仿真 |
| 草据/草捏/草厚 | 掌握 |
| 引少/亏染 | 减少/污染 |
| 校%/校强 | 较强 |
| 解略性/解路性 | 战略/前沿 |
| 肢术/恢术 | 技术 |
| 新琳/经娜 | 现场/经验 |
| 智糖/智胁 | 智慧 |
| 白激/白度 | 自驱 |
| 路俩/路情 | 热情 |
| 路质/路历 | 熟悉 |
| 独能/驻立 | 独立/技能 |
| 顶目/顺目 | 项目 |
| 供密错/供应鞋 | 供应链 |
| 章更多/真下 | 争取更多/拿下 |
| 打数/打致 | 打败 |

### Step 3: Generate Cleaned Excel

Use `openpyxl`. Key specs:
- Font: 微软雅黑, header 11pt bold white on #2F5496, body 10pt
- Zebra striping: even rows #D6E4F0
- Borders: thin #B4C6E7 all sides
- Column widths: 序号6, 部门12, 小组16, 岗位24, 职责55, 要求55, 在编10, 缺口10, 到岗14
- Row heights: header 28px, body 200-280px
- Freeze top row, auto-filter, summary row at bottom

### Step 4: Generate Word Analysis

Use `python-docx`. Report structure:

1. **Cover page** — title + date
2. **需求全景** — department维度 table + full position list table
3. **产品线分析** — ALD/PECVD vs PVD vs 可靠性 team
4. **招聘难度评估** — 红绿灯 table with 难度/岗位/原因/策略
5. **战略信号解读** — 4 key signals with analysis
6. **总结** — action items + time pressure

### Step 5: Analysis Dimensions (always cover)

- **Department**: 工艺 / 系统工程 / PVD breakdown
- **Function**: 机械设计 / 工艺 / 设备 / 材料 / 可靠性 / 磁控
- **Level**: 总监 → 经理 → 资深 → 高级 → 工程师
- **Urgency**: deadline-based red/yellow/green
- **Difficulty**: talent pool size estimation with specific company targets
- **Strategy**: what the hiring plan signals about company direction
- **Competitor mapping**: For semiconductor equipment roles, map AMAT/LAM/TEL/北方华创/中微/拓荆/盛美 as Tier 1-3 target companies

## Step 6: Save to DB

After analysis, save positions to `talent_pool.db` → `positions` table:
```sql
INSERT INTO positions (client, department, team, title, gap, deadline, level, education)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
```

This enables downstream: App 显示在招岗位, "多渠道搜 {客户}" 自动读岗位列表。

## Dependencies

```bash
python3 -m pip install openpyxl python-docx
```

## Output Files

- `招聘需求表_整理版.xlsx` — cleaned, formatted Excel
- `招聘需求分析报告.docx` — structured Word report
