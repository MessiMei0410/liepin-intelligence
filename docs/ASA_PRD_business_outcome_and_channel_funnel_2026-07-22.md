# ASA PRD：业务终态全链路一致性 + 渠道寻访漏斗可观测性

日期：2026-07-22
撰写：Kimi（基于 ASA_APP_KIMI_HANDOFF_2026-07-22.md、当日 Kimi 会话变更记录与线上代码核实）
执行方：Kimi CLI（或后续接手 Agent）
优先级：P0.5（介于已完成的阶段 1 与原阶段 3 之间）

---

## 0. 背景与核实依据

本 PRD 基于以下已核实事实（2026-07-22 实测）：

1. **业务终态（business_outcome）后端已存在但未全链路透传**：
   - DB schema：`agent_workflows.business_outcome`、`agent_goals.business_outcome` 已建列（`scripts/a_system_agent/schema.py:435,454,587-588`）。
   - 判定函数：`classify_business_outcome()`（`scripts/a_system_agent/workflow.py:109`），引擎在收尾时写入（`workflow.py:1258-1280`）。
   - 回填脚本：`scripts/backfill_business_outcome.py`（dry-run / --apply）。
   - 工作流详情 API `GET /api/v1/workflows/{id}` 已返回 `business_outcome`（实测 `workflow_1076e0e1d5d5` 返回 `completed_needs_review`）。
   - 前端 `src/workflow/statusMapping.ts` 已消费四枚枚举并映射中文文案。
   - **缺口**：`GET /api/v1/dashboard` 的 `workflows` 列表项**不含** `business_outcome`（实测返回仅 `workflow_id/status/current_stage/updated_at/title/progress`，见 `scripts/asa_core/service.py:260-298`）；Copilot、浮窗、扩展是否消费该字段未验证，存在"详情页说人话、总览和 Copilot 说 blocked"的口径分裂风险。

2. **渠道抓取质量数据已产生但未持久化、不可查询**：
   - 猎聘 runner `scripts/run_published_position_search.py` 与 X-SaaS runner `scripts/xsaas_candidate_search.py` 已为每个候选人计算 `resume_capture_status`（complete/partial/failed + missing + error）并聚合成 stats（如 `xsaas_candidate_search.py:238-245`、`run_published_position_search.py:339-393`）。
   - **缺口**：这些 stats 只嵌在步骤大输出/audit stdout 里（交接文档 11.8），没有持久化到工作流粒度的可查结构；X-SaaS 第 3 轮六组关键词返回 0 无法区分"真实无结果 / session 失效 / 查询拼接错 / 页面结构变化 / loading 未完成"（交接文档 11.9）。

3. 业务动机：士兰微 #154 已连续 3 轮寻访，第 2、3 轮均为"步骤 100% 完成但合格人数不足"。用户（猎头）最需要回答的问题是：**是渠道没抓到、抓取不完整、还是策略本身找不到人**。当前系统无法直接回答。

---

## PRD-1：business_outcome 全链路一致性

### 1.1 目标

`business_outcome` 成为工作流业务终态的**唯一事实源**，所有消费端（总览、工作流详情、Copilot、浮窗、扩展）口径一致；任何界面不得再把"人数不足"类业务终态展示为技术故障。

### 1.2 需求项

**R1.1 dashboard 摘要接口补字段**
- `GET /api/v1/dashboard` 的 `workflows[]` 每项增加 `business_outcome`（string | null）。
- 实现位置：`scripts/asa_core/service.py` 的 `dashboard()`（约 260-298 行），workflows 查询 JOIN 或补查 `agent_workflows.business_outcome`。
- 前端总览页工作流卡片统一走 `src/workflow/statusMapping.ts` 渲染，禁止在总览另写本地映射。

**R1.2 Copilot 消费业务终态**
- Copilot 回答涉及工作流状态时（如"这轮寻访怎么样了"），必须以 `business_outcome` 为准生成业务语言解释，与界面文案同源。
- 实现建议：A System Agent 组装 Copilot 上下文时注入 `business_outcome` 及其中文语义（复用 `classify_business_outcome` 的口径，不在 prompt 里新造第三套映射）。
- 明确写入/询问句区分等既有规则不得回归。

**R1.3 枚举契约冻结**
- 四枚枚举冻结为接口契约：`completed_target_met` / `completed_needs_review` / `completed_pool_insufficient` / `failed_technical`。
- 新增枚举值必须同时更新：`workflow.py` 判定、`statusMapping.ts`、`api.d.ts` 生成类型、契约测试。后端返回未知值时前端必须回落 status 原逻辑（现有行为，写进契约测试）。
- 在 `scripts/asa_core/app.py` 或 service 层加一个轻量断言/测试：API 输出的 `business_outcome` 只在枚举集 ∪ {null} 内。

**R1.4 存量一致性校验**
- 跑一次 `backfill_business_outcome.py --db <v3 db> --apply` 前必须先 dry-run 并把输出贴入 PR 描述；只 UPDATE `business_outcome` 列，不触碰 `updated_at`（脚本现有行为，保持）。

### 1.3 验收标准

- [ ] `curl /api/v1/dashboard` 的每个 workflow 项含 `business_outcome`，且与同 id 的 `/api/v1/workflows/{id}` 返回值一致。
- [ ] App 总览页第 2、3 轮寻访显示"本轮完成，合格人数不足…"而非"已阻塞"。
- [ ] 在 App 内向 Copilot 询问"士兰微第 3 轮寻访什么结果"，回答与界面终态文案语义一致，不出现"执行失败/系统故障"类表述。
- [ ] 新增契约测试：构造未知 `business_outcome` 值，前端回落 status 逻辑；Core 测试覆盖 dashboard 新字段。
- [ ] `npm run ci` 全绿；Core 31+ 项测试通过；A 系统回归守卫 `failure_count: 0`。

---

## PRD-2：渠道寻访漏斗可观测性

### 2.1 目标

每一轮寻访、每一个渠道（猎聘 / X-SaaS）的漏斗数据**持久化、可查询、可解释**：

```
query 组数 → 召回数 → 详情抓取尝试 → complete / partial / failed → 排重(已存在) → 新增入库
```

0 召回或 0 入库必须带可解释原因，不允许只显示"completed"。

### 2.2 数据模型

新表 `agent_workflow_channel_metrics`（建在 `scripts/a_system_agent/schema.py`，走既有 `_ensure_column`/建表迁移惯例）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | |
| workflow_id | TEXT NOT NULL | 关联 agent_workflows |
| step_key | TEXT | 产生数据的步骤（如寻访执行步骤） |
| channel | TEXT NOT NULL | `liepin` / `xsaas` |
| query_count | INTEGER | 本轮关键词/查询组数 |
| recall_count | INTEGER | 列表召回候选人条数 |
| detail_attempted | INTEGER | 发起详情页抓取数 |
| capture_complete | INTEGER | resume_capture_status=complete 数 |
| capture_partial | INTEGER | partial 数 |
| capture_failed | INTEGER | failed 数（含失败原因 Top 摘要，见 error_digest） |
| dedup_existing | INTEGER | 排重命中已有候选人数 |
| intake_new | INTEGER | 本轮实际新增入库数 |
| zero_result_reason | TEXT | 枚举，见 2.3；无 0 结果问题时为 null |
| error_digest | TEXT | JSON：失败原因聚合，如 `{"session_expired": 2, "detail_timeout": 1}` |
| runner_version | TEXT | 执行器/扩展版本（如扩展 0.3.11），便于回溯页面结构变化 |
| created_at / updated_at | TEXT | |

约束：`(workflow_id, step_key, channel)` 唯一；同一轮重试同一渠道时更新而不是追加歧义行；历史行保留（重试产生新 step_key 或显式 round 标记）。

### 2.3 0 结果原因枚举（zero_result_reason）

| 枚举 | 中文展示 | 判定来源 |
| --- | --- | --- |
| `no_results` | 渠道真实无匹配结果 | 列表页正常渲染但结果数为 0 |
| `session_invalid` | 登录态失效 | 检测到登录跳转/鉴权失败 |
| `query_build_error` | 查询词拼接异常 | 请求参数与策略输入不一致/为空 |
| `page_structure_changed` | 页面结构变化，解析失败 | DOM guard/parser 命中 fallback |
| `loading_timeout` | 页面加载未完成 | loading 超时或列表骨架未消失 |
| `unknown` | 原因待排查 | 以上均不满足（必须同时写 error_digest） |

判定逻辑放在 runner 内（猎聘 `run_published_position_search.py`、X-SaaS `xsaas_candidate_search.py`），runner 已经掌握页面状态；不要把猜测逻辑放前端。

### 2.4 写入与读取

**写入**：
- 两个 runner 在现有 stats 聚合点（`xsaas_candidate_search.py:238-245`、`run_published_position_search.py:339-393` 附近）额外产出标准化的 `channel_metrics` 字典。
- 由调用方（工作流引擎/服务层）写入新表。写入必须复用既有事务与审计惯例；失败不阻断主链路（metrics 丢失可告警不可阻塞寻访）。
- OpenCLI 影子链路**不写入**此表（维持 read_only_shadow 契约）；其聚合指标继续走自己的差异哈希通道。

**读取**：
- `GET /api/v1/workflows/{id}` 响应增加 `channel_funnel: [...]`（数组，每项对应一行 metrics，字段名同表列）。
- 摘要足够小，直接随详情返回即可；**不要**为此在每次轮询里带回原始 audit stdout（交接文档 11.8 的瘦身方向保持不变，本 PRD 不扩大传输面）。

### 2.5 前端展示

- 工作流详情页在"本轮新增/已评估"区域旁增加渠道漏斗条：每个渠道一行，`召回 X → 详情 Y（完整 a / 部分 b / 失败 c）→ 排重 d → 入库 e`。
- `zero_result_reason` 非空时显示对应中文解释 + error_digest 摘要，替代"步骤完成但 0 结果"的困惑态。
- 复用 `statusMapping.ts` 的语义色调体系；漏斗数字区域样式克制，不新增卡片套卡片。
- 总览卡片不显示漏斗（保持信息密度），仅工作流详情展示。

### 2.6 Copilot 消费

- 用户问"这轮为什么没找到人"时，Copilot 应能引用漏斗数据回答（如"X-SaaS 六组关键词召回为 0，原因是登录态失效；猎聘召回 5 条、3 条完整入库"）。
- 回答必须基于表中数据，禁止编造数字；无 metrics 的历史工作流明确说明"该轮未记录渠道明细"。

### 2.7 验收标准

- [ ] 新跑一轮真实寻访（可走 #154 第 4 轮）后，数据库新表有猎聘、X-SaaS 各一行完整漏斗数据，与 audit 日志中的 stats 一致。
- [ ] 构造 X-SaaS 0 召回场景（可用测试 fixture/回放），`zero_result_reason` 正确分类且前端显示中文解释。
- [ ] `GET /api/v1/workflows/{id}` 返回 `channel_funnel`；历史无数据工作流返回空数组且不报错。
- [ ] 漏斗各数字守恒：detail_attempted = complete + partial + failed；intake_new ≤ capture_complete。
- [ ] OpenCLI 影子产物仍不进入本表、不参与 intake/触达/写库。
- [ ] Core 测试覆盖：建表迁移幂等、写入唯一约束、API 序列化、0 结果分类；前端 Vitest 覆盖漏斗渲染与回落。
- [ ] `npm run ci` 全绿；Core 测试通过；A 系统回归守卫通过。

---

## 3. 实施顺序建议

1. **PRD-1 R1.1 + R1.4**（dashboard 字段 + 回填校验）——改动最小、收益立竿见影，先消除总览口径分裂。
2. **PRD-2 数据模型 + 写入**（runner 产出 + 新表）——先于前端做，数据先开始积累。
3. **PRD-2 API + 前端漏斗 + 0 结果解释**。
4. **PRD-1 R1.2 + PRD-2 §2.6**（Copilot 消费）——模型侧改动放最后，配意图回归用例一起上。

每一步独立可回滚、独立过守卫，不要把四步压成一个 commit。

## 4. 约束与禁止事项（继承交接文档，强调相关项）

- 所有真实候选人写入继续走 preflight/commit、幂等、审计和 A 系统同步；本 PRD 不新增任何绕过路径。
- OpenCLI 保持 `read_only_shadow`：不写入 `agent_workflow_channel_metrics`，不进 intake/触达/写库。
- 不要把搜索卡片摘要当完整简历；partial/failed 不进正式 intake（漏斗只观测，不改变 intake 门槛）。
- `status` 枚举与 `blocked` 状态机语义不变：本 PRD 只增加业务终态透传与观测数据，不重排状态机。
- 任何岗位、候选人、指标变化后照常跑 A 系统同步与回归守卫。
- 扩展版本若涉及 runner 侧改动（如 zero_result 判定需要扩展配合），按交接文档第 15 节升版本号、重载、跑 DOM guard。

## 5. 明确不做（本期）

- 不做"调整条件再搜"的智能策略建议（基于失败证据自动修订寻访策略）——单独立项，依赖本 PRD 的漏斗数据积累。
- 不做工作流大输出瘦身（11.8）、轮询优化（11.7）、main.tsx 拆分（11.2）——按原阶段 2/3 推进。
- 不改上下文选举评分体系（微信 activation 抢焦点问题沿用变更记录第 3 条的建议单独处理）。
