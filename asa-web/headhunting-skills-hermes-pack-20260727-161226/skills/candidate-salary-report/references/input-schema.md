# Salary Report Input Schema

Use this JSON shape for `scripts/build_salary_report.py`. Amounts may be strings or numbers; strings are preferred to avoid floating point drift.

```json
{
  "title": "候选人薪资流水报告",
  "candidate_name": "待补充",
  "report_date": "2026-06-16",
  "currency": "人民币元",
  "period": {
    "start": "2025-06",
    "end": "2026-05",
    "label": "最近12个月"
  },
  "source_summary": "个人所得税App截图及银行流水截图",
  "primary_payer": "某公司",
  "records": [
    {
      "month": "2026-05",
      "category": "工资薪金",
      "subtype": "正常工资薪金",
      "payer": "某公司",
      "income": "60485.64",
      "tax": "8841.75",
      "note": "正常工资薪金",
      "source": "截图1"
    }
  ],
  "bank_entries": [
    {
      "date": "2025-08-01",
      "summary": "他行汇入",
      "payer": "某公共服务中心",
      "account": "工银借记卡****",
      "amount": "320000.00",
      "balance": "326120.44",
      "note": "性质待核实",
      "source": "银行截图1",
      "include_in_main_totals": false
    }
  ],
  "source_images": [
    {
      "label": "图1",
      "caption": "2026-05工资及奖金截图",
      "path": "/absolute/path/to/image.jpg"
    }
  ],
  "known_context": [
    "2025-05有历史奖金260000元，但不属于本次最近12个月统计口径。"
  ],
  "custom_findings": [
    {
      "title": "固定薪资稳定性",
      "text": "大部分月份工资薪金约6万元。"
    }
  ],
  "custom_questions": [
    {
      "topic": "奖金规则",
      "question": "请确认全年一次性奖金的对应年度、发放规则、是否完整发放。"
    }
  ]
}
```

## Field Notes

- `records` are the main income/tax records included in the report totals.
- `bank_entries` are supplemental evidence. They are excluded from main totals unless `include_in_main_totals` is true; only set true when the user explicitly wants that treatment.
- `category` should keep human-readable labels, commonly `工资薪金`, `全年一次性奖金`, `偶然所得`, `补贴/津贴`, `股权/长期激励`, or `其他`.
- `month` must be `YYYY-MM`; `date` must be `YYYY-MM-DD`.
- `source_images.path` should be absolute whenever possible so the DOCX builder can embed the image.
- `custom_findings` and `custom_questions` are optional. The script also creates default observations/questions from the records.

## Extraction Checklist

For each visible payment, capture:

- income month or payment date
- income category and subtype
- payer / withholding agent
- gross income
- declared tax or withholding tax
- source screenshot/page
- whether it belongs to the analysis period
- whether it is recurring, one-time, supplemental, or unclear

For each bank inflow, capture:

- date, payer, account tail, amount, balance if visible
- transfer summary
- whether it corresponds to a tax record
- whether it should be included in compensation totals
- service-period, clawback, tax, or residency restrictions if mentioned
