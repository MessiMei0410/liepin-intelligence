---
name: candidate-salary-report
description: Create candidate salary verification reports and recruiter follow-up checklists from tax screenshots, payroll records, bank statement evidence, and compensation notes. Use when asked to make a 薪资报告, 候选人薪资流水报告, 薪资核实清单, offer salary evidence report, compensation verification memo, or a salary communication checklist for headhunting/recruiting workflows.
---

# Candidate Salary Report

## Workflow

1. Collect evidence: tax app screenshots, salary slips, bank statement screenshots, bonus notices, equity/RSU notes, and any user-supplied target period.
2. Extract the evidence into structured records. Prefer OCR or manual transcription with cross-checking over guessing from unclear screenshots.
3. Normalize the records using `references/input-schema.md`.
4. Run `scripts/build_salary_report.py` to generate the DOCX report and optional communication checklist.
5. Verify totals, period boundaries, included/excluded items, and attachment ordering before finalizing.

## Evidence Handling

- Treat tax-app income and bank inflows as different evidence types unless the user confirms they refer to the same payment.
- Separate recurring salary from non-recurring items such as annual bonus, government/talent subsidy, accidental income, sign-on bonus, stock proceeds, reimbursements, or one-time awards.
- Do not merge bank inflows into income totals by default. List them as supplemental evidence and mark them "待核实" unless the source and tax treatment are clear.
- Use the user's requested period. If absent, default to the latest contiguous 12 months visible in the evidence.
- Preserve source traceability: each material line item should point to a screenshot/file, page, or note when available.
- Flag gaps, duplicate-looking payments, unusually low/high salary months, unclear tax treatment, service-period/repayment risk, and missing equity/bonus evidence.

## Report Outputs

Produce one or both outputs depending on the request:

- Salary report: Word document with scope, core conclusions, income/tax totals, monthly breakdown, category breakdown, risk observations, follow-up questions, limitations, and source appendix.
- Communication checklist: Word document focused on recruiter questions, open issues, and blank notes fields for candidate calls.

Use concise Chinese business writing when the source material or user request is Chinese. Keep tone factual and avoid presenting estimated compensation as audited proof.

## Script Quick Start

Create a JSON file following `references/input-schema.md`, then run:

```bash
python3 /Users/messi/.codex/skills/candidate-salary-report/scripts/build_salary_report.py \
  --input /path/to/salary_data.json \
  --output-dir /path/to/outputs
```

Useful options:

```bash
--report-only
--checklist-only
--report-name 候选人薪资流水报告_近12个月.docx
--checklist-name 薪资沟通核实清单.docx
```

If the request includes images but no structured data, first create the JSON in the task's `work/` directory. Keep source images in their original location or copy them into a local `work/source_images/` folder; store their paths in the JSON.

## Validation

Before responding:

- Recalculate total income, tax, and net amounts independently from the JSON.
- Confirm category totals equal the record-level totals.
- Confirm the analysis period includes exactly the intended months and excludes historical/context-only items.
- Open or render the DOCX when practical to check layout, CJK fonts, table width, chart rendering, and image appendix visibility.
- State any unresolved assumptions in the final summary.
