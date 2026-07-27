---
name: headhunting-copilot
description: "Top-level copilot/router for Chinese headhunting and recruiting work. Use for any猎头/招聘需求, including岗位库/A系统/A 系统查询, 客户岗位查询, 长越科技/士兰微等客户岗位, 招聘/猎头任务分析, JD intake, 寻访策略, 候选人搜索, 简历筛选, 人岗匹配, 推荐报告, 面试跟进, 谈薪, offer决策, 入职跟进, 人才库复盘, or when combining GitHub recruiter skills with local Hermes/Codex skills."
---

# Headhunting Copilot

Use this as the top-level intake and routing skill for headhunting work. First identify the recruiting stage, then invoke the smallest relevant local skill(s) or Hermes CLI command needed to complete the task.

## Core Rule

When the user says "让 Hermes 分析", "让 Hermes 看一下", "用 Hermes", or similar, use the local Hermes CLI:

```bash
/Users/messi/.local/bin/hermes chat -q "..."
```

Do not call Codex subagents "Hermes". Do not route to external agents when the user asked for Hermes.

## Triage

Classify the request before acting:

| User intent | Primary route |
| --- | --- |
| A 系统 / 岗位库 / 客户岗位 / 客户候选池 query or update | `a-system-workbench` |
| New position, JD analysis, search plan | `headhunting-search-strategy` |
| End-to-end stage routing | `headhunting-workflow` |
| Multi-channel sourcing for a client | `multi-channel-search` |
| Liepin-only sourcing | `liepin-cdp-search` or `headhunt-liepin` |
| Save/query/update candidates | `talent-pool` |
| Export Liepin resume to docx | `resume-docx-export` |
| Candidate-job matching analysis | `candidate-matching-report` |
| Client recommendation report | `jiashi-recommendation-report` when available, otherwise make a structured report directly |
| Salary evidence verification | `candidate-salary-report` |
| Salary negotiation feedback | `salary-negotiation-feedback` |
| Job posting | `liepin-job-publish` or `xiaohongshu-job-publish` |
| Headhunting content/post | `headhunt-note-generator` |

If a task spans stages, start with `headhunting-workflow`, then load the stage-specific skill.

## Operating Workflow

1. Extract task state: client, position, candidate, current stage, available files, desired output, urgency.
2. Check local context before guessing: existing files under `~/Desktop/客户项目/{客户}/`, talent pool records, prior strategies/reports, and user-provided notes.
3. Route to the relevant skill. If a matching skill exists, read its `SKILL.md` and follow it instead of inventing a parallel workflow.
4. Apply recruiter methodology only where it improves the concrete deliverable: intake questions, search mapping, screening rubric, communication scripts, compliance checks, pipeline metrics.
5. Produce a usable artifact or concrete action: docx/report/html, database update, search result summary, call script, next-step checklist, or Hermes CLI analysis.
6. Validate the result at the level of risk: open generated files when practical, query the database after writes, recalculate salary totals, or sanity-check candidate counts.

## Recruiter Methodology

Read `references/recruiter-methodology.md` when the user asks for:

- intake questions or JD calibration
- Boolean/source search strategy beyond local templates
- candidate scorecards or screening rubrics
- interview plan or interviewer feedback forms
- offer/close strategy and pipeline conversion review
- compliance or fairness guardrails

Keep the output in concise Chinese business writing unless the user requests English.

## Stage Playbook

### 1. Intake and JD Calibration

For a new role, collect or infer only safe basics:

- business reason for the hire
- must-have requirements
- target companies and anti-targets
- level, location, compensation range, reporting line
- interview process and decision maker
- sell points and known objections

If JD is missing, ask only the minimum blocking questions or follow `headhunting-search-strategy` fallback rules.

### 2. Search and Sourcing

Prefer data-driven sourcing:

1. Query `talent-pool` for prior candidates by client/position/domain.
2. Reuse same-client strategies from `~/Desktop/客户项目/{客户}/`.
3. Build target company tiers and keyword variants.
4. Use `multi-channel-search` for client-wide searches and `liepin-cdp-search` for focused Liepin execution.
5. Save candidates and links through the local talent-pool workflow.

Avoid searching on protected characteristics. Use job-related criteria only.

### 3. Screening and Recommendation

Screen against:

- hard gates: education, years, industry, required tools/processes, language/location where relevant
- role fit: scope, complexity, seniority, ownership
- evidence: concrete resume lines, project outcomes, business impact
- risks: stability, compensation gap, commute/location, non-compete, competing offers

For candidate-job analysis, use `candidate-matching-report`. For client submission packs, use the local recommendation-report skill if installed.

### 4. Interview and Feedback

Create stage-specific notes:

- what the interviewer must verify
- evidence to seek
- red flags
- follow-up questions
- candidate motivation and objections

Update `talent-pool` status after meaningful stage changes.

### 5. Compensation, Offer, and Close

Use `candidate-salary-report` for evidence-based compensation verification and `salary-negotiation-feedback` for conversation summaries.

Separate:

- negotiation: package is not settled
- decision coaching: candidate hesitates due to non-salary concerns
- offer: terms are agreed and formal approval/letter is next

Always flag unresolved assumptions, missing evidence, competing offers, repayment or non-compete risk, and timing risk.

## Output Defaults

- Save client-facing project artifacts under `~/Desktop/客户项目/{客户}/` when following local skills.
- Save talent/candidate notes under `~/Desktop/人才库/` when following local skills.
- Use `.docx` for reports intended to be sent externally, `.md` for internal notes, and `.html` for interactive search summaries.
- Keep names, companies, and evidence traceable. Do not fabricate candidate facts.

## Quality Bar

Before finalizing:

- Confirm the output matches the requested stage and deliverable.
- Mention which local skill or Hermes command was used.
- Report file paths for generated artifacts.
- If no local action was needed, give a direct answer without pretending work was performed.
