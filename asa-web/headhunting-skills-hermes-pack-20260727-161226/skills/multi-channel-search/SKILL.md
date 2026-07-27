---
name: multi-channel-search
description: 多渠道候选人寻访。默认按 A 系统 v3 标准岗位执行猎聘 + X-SaaS 岗位寻访、历史排重、待复核入库和同步审计；仅在用户明确要求公司人才地图时使用旧的公司名变体报告模式。
version: 2.0.0
---

# 多渠道候选人寻访 v2

## 模式路由

### A 系统岗位模式（默认）

满足任一条件即使用本模式：

- 用户给出客户和岗位。
- v3 `positions` 能唯一定位在招岗位。
- 请求涉及 A 系统、岗位补搜、继续找人、A/B 触达或岗位推进。

先运行 A 系统 Cognee 启动召回，并遵守 `a-system-workbench`：

```bash
/Users/messi/Documents/Codex/2026-07-03/gi/work/a_system_cognee_memory.py startup
```

### 公司人才地图模式（显式 legacy）

仅当用户明确说“搜某公司员工”“做竞品人才地图”“按公司名变体拉名单”时使用。该模式可以生成独立 HTML 报告，但不能冒充 A 系统岗位寻访结果。

如果 v3 中存在多个同名或相近岗位，不得从历史 `candidates.position` 猜测，必须先确认标准岗位。

## A 系统岗位模式

### Step 1: 解析岗位和历史排除集

v3 DB 是唯一事实源：

```text
/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db
```

运行：

```bash
python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py \
  context --client 客户名 --job 岗位名
```

必须从 `positions`、`position_profiles`、`jobs` 解析标准岗位，并召回：

- 已存在候选人及本地 candidate ID。
- 已触达、已推荐、待复核候选人。
- 最新人工停止、`H5`、`screen_rejected`、`rejected` 人选。
- X-SaaS ID 与姓名 + 公司 + 职位身份依据。

最新人工 `stop` 优先于旧的触达事实。脱敏姓名不能单独用于判重。

### Step 2: 生成岗位驱动搜索计划

```bash
python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py \
  plan --client 客户名 --job 岗位名 --max-queries 6
```

计划必须来自岗位画像：

- `search_keywords_json`：核心能力组合。
- `target_companies_json`：目标公司 + 能力证据组合。
- `exclusion_tags_json`：搜索后复核排除项。
- 历史停止原因：下一轮复核门槛。
- `search_experiments`：`learned + recommended_count=0` 的词默认跳过。

不得内置 AMHS、PQE、IE、Etch 等固定岗位分类。目标公司只用于扩池，不能单独决定 A/B。

### Step 3: 渠道预检

```bash
python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py \
  preflight --client 客户名 --job 岗位名 --port 9223
```

硬规则：

- 猎聘和 X-SaaS 登录页必须记为 `login_required`，不能记成 0 结果。
- 猎聘搜索后必须核对搜索框值、结果数和相关卡片；`3000+` 且无相关卡片是泛化推荐流。
- X-SaaS 必须核对 URL 中的当前关键词，旧 hash/缓存结果记为 `stale_query`。
- 任一渠道被阻断时可继续另一个渠道，但最终汇报必须单列失败渠道。

### Step 4: 分渠道执行搜索

猎聘使用 `liepin-cdp-search` 的当前会话链接和 DOM TextNode 解析规则。X-SaaS 使用完整 URL 导航，不能只修改 hash 后假设结果已刷新。

搜索卡片只负责召回。自动寻访 runner 在排重后必须逐一打开候选人详情页，抓取完整履历，再生成下游 intake JSON：

- 猎聘详情页使用当前会话 `resume_url`，先核对姓名 + 公司/职位身份，再保存 `full_text`、`work_text`、`project_text`、`education_text`。
- X-SaaS 详情页使用当前候选人 ID 导航，保存同样的完整字段；列表页的工作摘要不得冒充完整简历。
- 每条记录写入 `resume_capture_status=complete|partial|failed`、缺失分区和错误原因。`complete` 至少要求来源链接、100 字以上完整履历、工作经历和教育经历。
- `resume_capture_status` 存在且不是 `complete` 时，intake 必须拒绝该条记录并保留错误，不得静默写入一个内容残缺的人选。

每轮保存以下结构化证据：

```json
{
  "channel": "liepin|xsaas",
  "query": "实际提交的关键词",
  "name": "候选人",
  "company": "当前公司",
  "title": "当前职位",
  "education": "学历",
  "experience": "年限",
  "city": "城市",
  "profile_text": "卡片或完整简历证据",
  "work": [{"company": "公司", "title": "职位", "dates": "任职时间"}],
  "education_history": [{"school": "学校", "major": "专业", "degree": "学历", "dates": "就读时间"}],
  "res_id_encode": "猎聘当前会话候选人ID",
  "resume_url": "猎聘当前会话完整链接",
  "candidate_id": "X-SaaS ID"
}
```

公司和职位必须来自工作经历的 `公司 · 职位 · 时间` 结构。大学、学院、学校不能作为当前公司。

猎聘卡片链接优先从 `data-tlg-ext.res_id_encode` 读取并生成当前会话详情 URL；模拟点击只允许作为兜底。不得为了美化链接删除已有会话参数。抓到卡片但没有 `resume_url` 或 `res_id_encode` 时，必须标记来源链接缺失，不能静默当作完整结果。

### Step 5: 详情复核与分层

搜索卡片只用于召回，不能直接定 A/B。至少打开完整简历验证：

- 岗位硬门槛。
- 核心能力及项目深度。
- 年限、学历、地点、职级。
- 岗位画像排除项和历史高频停止原因。

建议分层：

- A：硬门槛通过，核心能力有完整项目证据。
- B：硬门槛基本通过，仅一项关键能力待确认。
- C/待核验：卡片命中但完整证据不足，不自动触达。
- 停止：硬门槛失败、方向不符、重复或人工停止。

### Step 6: 待复核入库

先把渠道结果保存为 JSON，再执行 dry-run：

```bash
python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py \
  intake --client 客户名 --job 岗位名 --input /absolute/path/candidates.json
```

确认以下结果后才允许加 `--apply`：

- `accepted_count`
- `existing_count`
- `batch_duplicate_count`
- `error_count`

```bash
python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py \
  intake --client 客户名 --job 岗位名 --input /absolute/path/candidates.json --apply
```

入库阶段只能使用：

- 猎聘：`S1 新增寻访/待复核`
- X-SaaS：`X1 X-SaaS新增/待复核`

不得在搜索入库时写成 `job_chat_verified`。`job_candidates.source_candidate_id` 必须是本地 v3 candidate ID；猎聘 URL 和 X-SaaS ID 仅作为事件证据。

每个新入库人选还必须：

- 写入 `source_profiles`，保存 `profile_text/full_text/work_text/education_text/source_url` 及渠道候选 ID。
- 写入或更新 `entity_source_links`，保证 ASA 人选详情出现可点击的猎聘/X-SaaS 来源入口。
- 在 `candidate_events.raw_json` 和 `source_id` 保留同一来源证据，供历史数据兜底和链接修复。
- 详情 API 必须能从 `source_profiles`、寻访事件或旧 `candidates.skills` 恢复卡片履历；缺少某一张表不得导致详情全空。

### Step 7: 人工复核和触达

- X-SaaS 复核通过进入 `X2 X-SaaS复核通过/待人工联系`，不能记为猎聘岗位触达。
- 猎聘 A/B 默认按 `liepin-cdp-search` 规则带目标岗位开聊，除非用户明确只搜不发。
- 只有页面证据成立时才能写 `job_chat_verified` 或 `job_recommended_verified`。
- `继续沟通` 本身不是目标岗位触达成功。
- 人工停止写 `resume_review_completed=stop` 和 `H5 最近寻访/初筛不通过`。

### Step 8: 同步、审计和汇报

任何真实入库、触达或停止后运行：

```bash
/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py \
  --client 客户名 --job 岗位名 --no-open
```

必须通过严格客户审计和 A 系统回归检查。最终汇报按渠道列出：搜索轮次、查看数、新增数、重复数、停止数、触达成功数及阻断原因。

A 系统岗位模式默认不生成独立营销式 HTML；A 系统页面和结构化搜索回执是交付面。

## 公司人才地图模式（legacy）

本模式保留旧能力：

1. 收集公司全称、简称、英文名和历史名称。
2. 猎聘与 X-SaaS 按公司名变体搜索。
3. 输出渠道分布、岗位分布和可筛选 HTML 报告。
4. 不直接写 A 系统岗位推进关系，除非随后明确映射到标准客户/岗位并走 A 系统 intake。

参考资料仍位于 `references/company-variants.md`，HTML 模板位于 `templates/`。

## 验证

```bash
python3 -m unittest discover \
  -s /Users/messi/.codex/skills/multi-channel-search/tests -v
```

验收项：

- 岗位来自 v3 `positions` / `position_profiles`。
- 最新人工停止状态不会被旧触达覆盖。
- 登录失败、推荐流和缓存结果不会记为零结果。
- dry-run 不修改数据库。
- 真实 intake 同步写入 A 系统所有候选人表面。
- 新增人选的 `source_profiles` 有非空履历，`entity_source_links` 有可点击来源 URL。
- 新增人选的 `source_profiles` 必须优先保存详情页完整履历；仅列表摘要的记录应停留在 intake 错误清单。
- 同一人同一岗位不生成第二条推进关系。
- A 系统同步、严格审计和回归检查通过。
