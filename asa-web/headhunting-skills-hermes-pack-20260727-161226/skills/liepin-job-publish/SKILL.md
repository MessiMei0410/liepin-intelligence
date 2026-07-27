---
name: liepin-job-publish
description: Publish or list headhunting jobs on Liepin (猎聘) from a reviewed JSON draft using Chrome CDP automation. Use when the user asks to 发岗位, 发布岗位, 上架岗位, 在猎聘发布/上架职位, 自动填猎聘职位表单, or verify a Liepin job has been published.
---

# Liepin Job Publish

## Overview

Use this skill to主导猎聘职位发布：把用户确认过的岗位信息整理成 JSON 草稿，打开猎聘发职位页，先填表不发布，读回关键字段，获得明确授权后发布，并到职位列表验证。

Default to Chrome CDP on port `9223` with the persistent profile `~/.hermes/chrome_profile_xhs`. Never attempt to automate passwords, SMS codes, or login challenges; if Liepin redirects to login, ask the user to log in manually and continue afterward.

## Resources

- `scripts/liepin_publish_job.py`: safe publish helper. Default mode is `prepare`; real publishing requires `--mode publish --confirm PUBLISH`.
- `references/job-draft-template.json`: JSON draft schema/example.
- `references/publish-checklist.md`: field readback and post-publish checklist. Read this before every real publish.

## Workflow

1. Create or update a job draft JSON from the user's JD and constraints. Use `references/job-draft-template.json` as the schema.
   - 【草稿发布前强制自检，每次生成后逐条过】:
     1. client_company 是否为实际公司全称(非分类描述词如半导体CIP)? 多岗位是否一致?
     2. city_keyword/city_choice 是否非空?
     3. private_job 是否为 false?(用户从未要求保密)
     4. job_title 是否与用户要求的完全一致? 两岗位标题是否搞混?
2. Ensure Chrome CDP is running. If needed, start Chrome:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9223 \
  --user-data-dir="$HOME/.hermes/chrome_profile_xhs" \
  --no-first-run \
  --no-default-browser-check \
  "https://h.liepin.com/job/showaddpage/"
```

3. Confirm login by opening `https://h.liepin.com/job/showaddpage/`. If the tab is redirected to `account/login` or the page lacks form fields, stop and ask the user to log in.
4. Run `prepare` first:

```bash
python3 /Users/messi/.codex/skills/liepin-job-publish/scripts/liepin_publish_job.py \
  --mode prepare \
  --draft /path/to/job-draft.json \
  --log /path/to/liepin_prepare_log.json
```

5. Inspect the log and, when needed, the live page. Required fields must read back correctly: company, job title, job category, city, salary low/high/months, work year, education, industry, JD, recruit count, close date, private switch, agreement, publish button enabled.
6. Only after the user has asked to publish/上架/发岗位, run:

```bash
python3 /Users/messi/.codex/skills/liepin-job-publish/scripts/liepin_publish_job.py \
  --mode publish \
  --confirm PUBLISH \
  --draft /path/to/job-draft.json \
  --log /path/to/liepin_publish_log.json
```

7. Verify after publishing. Treat the click as insufficient; the job is complete only after one of these is true:
   - Result page says the job has been submitted/published/auditing, and there are no validation errors.
   - Job list page contains the job title and client company with an expected status.

Open the list page:

```text
https://h.liepin.com/job/showlistpage?jobStatus=11
```

## Draft Fields

Required fields:

- `client_company`: 猎聘代招企业名称。【硬规则】必须填实际公司全称(如微导纳米)，禁止填分类描述词(如半导体CIP)。多岗位共用同一公司时名称必须一致。生成草稿后立即与用户确认。
- `job_title`
- `job_category_keyword`, `job_category_choice`
- `city_keyword`, `city_choice` — 【硬规则】禁止为空。空城市导致发布时必报错。若用户未明确城市，必须追问。
- `salary_low_k`, `salary_high_k`, `salary_months`
- `work_year_keyword`, `work_year_choice`, `work_year_low`, `work_year_high`
- `education_choice`, `education_tongzhao`
- `industry_keyword`, `industry_choice`
- `private_job`
- `recruit_count`
- `close_date`
- `description`

**`private_job` 默认 `false`（职位公开）。**用户从未要求猎聘职位保密，此前多次因 `private_job: true` 翻车。仅在用户明确要求保密时才设为 `true`。

## Safety Rules

- Do not publish from incomplete or guessed critical fields. Ask or prepare only.
- Do not publish if the prepare log shows validation errors such as “请填写工作城市”.
- Do not claim success from `publish_click: ok` alone. Verify the result page or job list.
- Do not operate login credentials. Ask the user to log in manually.
- For live publishing, preserve logs under `outputs/` or a project-specific folder.

## Practical Notes

- Company and city fields are Ant Design controlled components. A dispatched DOM click can be a false positive; the script uses real CDP mouse events plus field readback.
- City `深圳` commonly appears as `广东·深圳` after selection.
- Liepin may anonymously display the company, e.g. `某深圳大型电子/半导体/集成电路公司`; still verify the underlying customer/company in the list row.
- If the helper opens a fresh tab while a half-filled tab already exists, verify the active tab before publishing.
