---
name: headhunting-search-strategy
description: "Formulate comprehensive search strategies for headhunting positions — analyze domain, map target companies, design keywords, draft communication scripts, and execute search. Optimized with 8 enhancements: experience accumulation, auto competitor analysis, JD fallback questionnaire, client template reuse, pre-search pool testing, keyword ambiguity warnings, dual-mode delivery, and cross-position association."
version: 2.1.0
author: Hermes Agent
license: MIT
---

# Headhunting Search Strategy Formulation

Trigger: user asks for a search strategy / 寻访策略 / 寻访方案 for a client position.

## Position Library First (岗位库优先)

Before analyzing, searching, reporting, or revising any position, read the local position library:

```sql
SELECT * FROM positions WHERE client = ? AND status = 'open';
SELECT * FROM position_profiles WHERE client = ?;
```

Rules:
- Treat `positions.title` and `position_profiles.position` as the canonical role names and directions.
- Use `position_profiles.source_position_ids_json` to connect strategy/profile rows back to `positions.id`.
- Do not infer active roles from `candidates.position` or `candidate_clients.position_tag`; those fields may contain old merged names or historical execution labels.
- For multi-role clients, build strategy and cross-position association from the position library first, then map historical candidates to those canonical roles.

## Mode Detection (区分模式 — 优化7)

Detect which mode the user wants BEFORE building the strategy:

| User says | Mode | Action |
|-----------|------|--------|
| "分析XX做寻访" / "做寻访" / "找人" | **Full-flow** | Strategy → Search → Evaluate → Save to talent pool |
| "出个策略" / "生成寻访策略" / "只出策略" | **Strategy-only** | Generate .docx only, stop and wait for user to say "开始搜" |
| "直接发我" | **Deliver-and-ask** | Generate .docx, send it, ask "要开始搜索吗？" |

**Default**: if ambiguous, ask "要我只出策略，还是直接开始搜？"

## Delivery: dual-path

### Full-flow path
1. Build strategy → save .docx only → send to user
2. **Immediately** load `multi-channel-search` → execute multi-channel search (猎聘变体 + X-SaaS四库)
3. Extract candidate cards → evaluate → generate result report (.md + .docx)
4. Generate standalone candidate links file (.md)
5. **Load `talent-pool`** → save all candidates with `INSERT OR IGNORE`
6. Append newly discovered pitfalls to `references/pitfalls/{domain}.md`
7. Update usage guide at `~/Desktop/客户项目/Hermes猎头工作流使用指南.md`

### Strategy-only path
1. Build strategy → save .docx → send to user
2. Ask: "要开始搜索吗？"

---

## Phase 0: Cross-Position Association (多职位关联 — 优化8)

Before building the strategy, check `~/Desktop/客户项目/{客户}/` for prior strategies for the same client:

```bash
ls ~/Desktop/客户项目/{客户}/*寻访策略*.md
```

If found:
- Read the most recent strategy for company background (reuse in Phase 1)
- Read the result reports to find cross-position candidates
- In the new strategy, add a section: `八、跨岗位候选人关联`

Example output:
```
八、跨岗位候选人关联

本客户同时寻访以下岗位（关联池可互推）：

| 岗位 | 策略日期 | 人才池 | 可能复用 |
|------|---------|--------|---------|
| 光学产品经理 | 2026-06-02 | 12人 | Zemax/光路人才懂精密运动？ |
| 运动台产品经理 | 2026-06-02 | 15人 | 隐冠半导体候选人兼通光学+运动 |

⚠️ 候选人 #A5 王**(镭望光学+SMEE): 光机结构+精密运动 — 两个岗位都可能匹配
```

---

## Phase 1: Research the Position

### 1a. Load client context (同客户模板复用 — 优化4)

Search for existing materials in order:

1. `~/Desktop/客户项目/{客户}/` — previous strategies, JDs, result reports
2. `~/Desktop/JD与行业报告/` — general JDs and industry reports
3. Session history for prior discussions about this client

If a prior strategy for the same client exists:
- **Reuse** the company background section verbatim
- **Reuse** the evaluation framework structure
- **Reference** the prior candidate pool in the cross-association section

### 1b. Auto competitor analysis (自动竞品分析 — 优化2)

Before mapping target companies, do web research:

```bash
# Search for competitors and supply chain
web_search("{company} {position_domain} competitors")
web_search("{position_domain} top companies China")
web_search("{company} supply chain partners")
```

Also check `headhunt-liepin/references/company-name-aliases.md` for any known company name variants across platforms (e.g., 鹏新旭 vs 深圳市鹏新旭技术有限公司 vs 鹏新旭(PST)). Different platforms use different names for the same company — the alias table helps ensure full coverage.

Use results to build Tier 1/2/3. Don't rely solely on memory — verify with real market data.

### 1c. JD fallback (JD缺失时主动问 — 优化3)

If no JD file exists, do NOT infer responsibilities. Instead, ask the user:

```
没有找到 {职位} 的 JD。请快速确认：
1. 核心职责是什么？（一句话）
2. 硬性门槛？（学历/专业/经验年限）
3. 有没有对标的目标公司？
4. 这个岗位和同客户其他岗位的关系？（如已有光学PM，这个是补充还是独立？）
```

Proceed with strategy only after user responds (or if user explicitly says "按职位名推").

---

## Phase 2: Analyze the Role

Clarify what makes this role distinct:
- Is it a pure engineering role, product management, or hybrid?
- What level? (individual contributor vs. team lead vs. director)
- What's the core differentiator?
- How does it relate to other positions at the same client? (多职位关联)

### 2a. Product-line / application-scene gate for semiconductor TME roles

For semiconductor TME / 技术市场 / FAE / AE / 产品市场 / 应用市场 roles, do not treat target company, title, or function label as sufficient fit evidence. The strategy must explicitly split:

- Product line: e.g. 三次电源, 多相, DrMOS, POL, MCU, sensor, RF, discrete power.
- Application market: e.g. PC, server, automotive, industrial, consumer.
- Customer scene: e.g. OEM/ODM, cloud/server platform, automotive Tier 1/OEM, module maker.

Target-company mapping is only a pool-expansion input. A-level requirements must name the required product line and application scene. If the JD is ambiguous, add verification questions instead of inferring fit from company/title alone.

---

## Phase 2.5: Pre-search Pool Testing (搜前测池 — 优化5)

**Optional but recommended for niche roles.** Before finalizing the strategy document, do a quick CDP ping to test keyword pool sizes:

```bash
# Launch Chrome if not running
bash ~/.hermes/scripts/chrome_cdp.sh

# Quick test: search R1 core term, get count only
# (No need to extract cards — just the total count)
```

If pool < 20: flag the strategy with "极难 5/5星" and suggest LinkedIn as supplement.
If pool > 500: suggest adding filters earlier.
If keyword noise > 40%: flag ambiguity and suggest alternatives.

Insert results into the strategy under:
```
四、搜索策略 → 4.0 关键词预检

| 关键词 | 猎聘结果 | 预估噪声率 | 建议 |
|--------|---------|-----------|------|
| 运动台 产品经理 | 176 | ~50%运动品牌 | 改用「精密运动+半导体」 |
```

---

## Phase 3: Build the Strategy

Standard sections:

```
一、岗位理解 — role analysis + differentiators
二、候选人画像 — hard requirements + soft requirements + one-line summary
三、目标公司Mapping — Tier 1/2/3 with cities
四、搜索策略 — keywords (human + execution formats), title variants, channels, filters
  4.0 关键词预检 — pool sizes and noise estimates (if Phase 2.5 was run)
  4.1 关键词组合
  4.2 职位名称变体
  4.3 渠道优先级
  4.4 猎聘高级筛选
  4.5 歧义预警 (搜索建议内置 — 优化6)
五、候选人沟通话术
六、评估要点
七、注意事项
八、跨岗位候选人关联 (if same client has multiple positions)
```

For semiconductor TME/FAE/product-market positions, the `二、候选人画像` and `六、评估要点` sections must include a three-layer gate:

| Layer | Required evidence |
|-------|-------------------|
| Product line | Specific product family handled by the candidate |
| Application market | End market/application the product served |
| Customer scene | Customer type and engagement mode |

Rating rule: A = all three layers match or are strongly evidenced; B = company/function match but one layer needs verification; C = benchmark company only or product/application mismatch. Example: benchmark-company candidate doing MCU automotive application is C for a PC three-phase-power TME role.

### 歧义预警表 (搜索建议内置 — 优化6)

For every keyword set, include an ambiguity assessment:

```
### 4.5 歧义预警

| 关键词 | 风险 | 替代方案 |
|--------|------|---------|
| 运动台 | 50%匹配运动品牌PM | 精密运动 半导体 |
| Aerotech | 匹配国产eVTOL公司 | PI 精密运动 或 ETEL |
| 纳米定位 | 低风险 | — |
```

---

## Phase 4: Deliver & Execute

1. Save `{客户}_{职位}_寻访策略_{日期}.docx` to `~/Desktop/客户项目/{客户}/` — **.docx only, no .md**
2. Send .docx to user
3. If full-flow mode: load `multi-channel-search` → execute multi-channel search
4. After search: generate HTML report (`候选人链接_点击跳转.html`)
5. Load `talent-pool` → save candidates (no res_id, name+company unique key)
6. Append newly discovered pitfalls to skill
7. Update usage guide at `~/Desktop/客户项目/Hermes猎头工作流使用指南.md`

---

## Phase 5: Experience Accumulation (经验积累 — 优化1)

After each completed search, append domain-specific pitfalls to the references:

```bash
# If new keyword ambiguity discovered
echo "- {keyword}: {noise_rate} noise, use {alternative} instead" >> references/pitfalls/{domain}.md

# If new company-name collision found  
echo "- {company}: matches {unrelated_domain}" >> references/pitfalls/{domain}.md

# If pool size insight gained
echo "- {position} pool: ~{N} nationwide, ~{M} in Shanghai/Suzhou (master+)" >> references/pitfalls/{domain}.md
```

Next time a similar domain is searched, read `references/pitfalls/{domain}.md` first to avoid known traps.

---

## Phase 6: Iteration Workflow (迭代工作流 — 优化1-7)

When a search has been done before for the same position and the user comes back:

### 6a. Detect iteration mode

Trigger phrases:
- "继续找" / "再搜一轮" / "更新策略" / "客户反馈了"
- "找更多像{候选人}这样的人"
- "客户说方向不对，要改成{new_direction}"

### 6b. Capture client feedback (📝 客户反馈追踪 — 优化2)

Before re-searching, ask structured questions:

```
客户反馈是什么？
1. 上次推荐的人里哪些被认可了？（方向对了）
2. 哪些被否决了？原因是什么？
3. 客户希望调整什么方向？
```

Update talent-pool:
- Approved candidates → `status='client_approved'`, `client_feedback='...'`
- Rejected candidates → `status='client_rejected'`, `elimination_reason='...'`

### 6c. Generate strategy changelog (📋 策略版本管理 — 优化1)

When re-searching, create a new strategy file with version suffix:

```
集萃苏科思_运动台产品经理_寻访策略_2026-06-02_v2.md
```

The v2 strategy must include a changelog section:

```
## 迭代记录

| 版本 | 日期 | 变更类型 | 变更内容 |
|------|------|---------|---------|
| v1.0 | 2026-06-02 | 初始策略 | 4轮搜索，30家目标公司 |
| v1.1 | 2026-06-05 | 客户反馈 | 客户认为精密运动方向OK，但希望更多电机/伺服背景人选 |

### 客户反馈详情

**认可方向**：
- 精密运动平台背景（华卓精科、SMEE人选被认可）
- 硕士以上学历

**调整方向**：
- 增加电机/伺服控制背景（汇川、埃斯顿等）
- 放宽城市限制，接受北京人选remote
- 不限定"产品经理"title，系统工程师也可考虑

### 已推荐排除

以下候选人已推荐给客户，本轮不再重复推荐：
- S1 张** (华卓精科) — 客户已联系
- S2 盛** (SMEE) — 等待反馈
- ...
```

### 6d. Incremental strategy update (🔄 增量更新 — 优化4)

Don't rewrite the full strategy. Only update affected sections:

| Client says | Update section |
|-------------|---------------|
| "方向OK，继续找" | 四、搜索策略 → 加关键词变体 |
| "学历放宽" | 二、候选人画像 → 改硬性要求 |
| "多找XX公司的人" | 三、目标公司Mapping → 升Tier |
| "不要YY背景" | 二、候选人画像 → 加排除条件 |
| "更像张**这样的" | 二、候选人画像 → 以anchor为模板 |

### 6e. Anchor-based search template (🎯 方向锚定 — 优化6)

When user says "找更多像{name}这样的人":

1. Query talent-pool for the anchor candidate's full profile
2. Mark as `anchor_candidate=1`
3. Extract search template:
   ```
   锚定候选人: 张** (华卓精科, 产品经理11年, 机械硕士)
   新搜索方向:
   - 公司: 华卓精科 + 同赛道公司(隐冠半导体/中科科仪)
   - 技能: 精密运动台 + 双工件台 + 机械设计
   - 学历: 机械/机电硕士+
   - 经验: 10年以上半导体设备
   ```
4. Add this as a new section in the v2 strategy: `四(附). 锚定搜索方案`

### 6f. Iteration comparison report (📊 迭代对比 — 优化7)

After re-search, generate a delta section in the result report:

```
## 迭代对比 (v1 → v2)

| 指标 | v1 | v2 | 变化 |
|------|----|----|------|
| 候选人总数 | 15 | 12 | -3 (排除已推荐) |
| 新增候选人 | — | 9 | 9个未见过的新面孔 |
| 来源公司 | 华卓精科/Akribis/SMEE | 汇川/埃斯顿/华卓精科 | +电机伺服赛道 |
| S级候选人数 | 4 | 2 | 精准但池小 |
| 搜索轮次 | 3 | 2 | 跳过R3(公司名歧义) |

### 新增候选人亮点
- xxx (汇川技术) — 电机伺服PM，新赛道首发
```

### 6g. Strategy-only mode for iterations (优化7 扩展)

When re-searching, the mode detection extends:

| User says | Mode |
|-----------|------|
| "客户反馈了，更新下策略" | Strategy-only: show changelog, wait for confirm |
| "按新方向继续找" | Full-flow: update strategy + search immediately |
| "客户说方向调整了，再搜一轮" | Full-flow: capture feedback → update → search |

---

## Phase 7: Update Usage Guide

After any skill modification, update `~/Desktop/客户项目/Hermes猎头工作流使用指南.md` and regenerate .docx.

## 关键词设计规范

Same as before — strategy uses boolean for readability, execution uses simple space-separated Chinese.

## Pitfalls (accumulated)

- Don't confuse 产品岗 with 技术岗
- Don't be generic — every position has a unique value proposition
- **Semiconductor TME/FAE roles require three-layer fit**: product line + application market + customer scene. Benchmark company/title is not enough for A-level; use it to expand the pool, then down-rank or exclude product/application mismatches.
- Save before sending
- Existing materials first
- Keyword ambiguity in niche technical roles
- Company-name searches unreliable on 猎聘
- Check references/pitfalls/{domain}.md before designing keywords for unfamiliar domains
- **AMHS/半导体搬送系统**: 极难寻访（5/5星），详见 `references/amhs-semiconductor-domain.md`
- Check references/amhs-domain-pitfalls.md for AMHS/半导体自动化岗位的特殊寻访挑战（极难岗，猎聘池极小）
- When client has multiple open positions, always check for cross-position candidates
- Distinguish strategy-only from full-flow mode — don't auto-search when user only asked for strategy
- **User can provide requirements as bullet points without a formal JD**: treat this as sufficient input — extract client, position, hard requirements, soft preferences, and budget directly
