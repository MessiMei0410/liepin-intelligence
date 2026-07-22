# ASA 交接任务包（第二轮）：业务终态透传收尾 + 寻访漏斗前端落地与验证

日期：2026-07-22 晚
撰写：Kimi（对线上代码与 API 逐项实测后修订）
执行方：Kimi CLI
前置阅读：`ASA_APP_KIMI_HANDOFF_2026-07-22.md`、`AGENTS.md`
关联文档：`docs/ASA_PRD_business_outcome_and_channel_funnel_2026-07-22.md`（原始 PRD，背景与验收思路参考；**其 PRD-2 数据模型部分已被本轮既有实现取代，以本文件为准**）

---

## 0. 核对结论（2026-07-22 22:59 实测）

本轮 Kimi CLI 已完成：R4 main.tsx 拆分、R5 类型落地、R6 Playwright E2E、R7 轮询切摘要+SSE、R9/R10、R12-b、R14、阶段 0 验证基线。**寻访漏斗的后端与 API 也已建成**：

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `agent_sourcing_funnel` 表（query/recall/dedupe/detail complete-partial-failed/intake dup+new/assessed/high_score/zero_attribution/error） | ✅ 已建 | `scripts/a_system_agent/schema.py:326-359` |
| 漏斗持久化 + 渠道失败尽力留行 | ✅ 已实现 | `capability_runtime.py:399-543`（`_persist_sourcing_funnel` / `_record_sourcing_funnel_failure`） |
| 0 结果归因（7 类） | ✅ 已实现 | `capability_runtime.py:69-99`（`classify_zero_result`） |
| 读接口 `GET /api/v1/workflows/{id}/sourcing-funnel` | ✅ 已上线 | `asa_core/app.py:187`、`service.py:808`；`api.d.ts` 已含类型 |
| 工作流 `/summary` 含 `business_outcome` | ✅ 已上线 | 实测 `workflow_1076e0e1d5d5` 返回 `completed_needs_review` |
| **dashboard `workflows[]` 透传 `business_outcome`** | ❌ **缺失** | 实测 `/api/v1/dashboard` 列表项仍只有 `workflow_id/status/current_stage/updated_at/title/progress` |
| **前端渲染渠道漏斗** | ❌ **缺失** | `src/` 中除生成的 `api.d.ts` 外无任何 sourcing-funnel 消费；`WorkflowPanel` 未展示 |
| **既有工作流的漏斗数据** | ❌ 空 | 实测 #154 第 3 轮返回 `channels: [], runs: []`（写入点在执行链路，旧轮次无回溯） |
| **Copilot 消费 business_outcome / 漏斗** | ❓ 未验证 | R12-b 后 React 为纯转发，需在 A System Agent 侧确认 grounding |

**重要约束**：漏斗数据模型以既有 `agent_sourcing_funnel` 表为准，**禁止另建 `agent_workflow_channel_metrics` 之类的新表**，禁止绕过 `_persist_sourcing_funnel` 另写一套归因逻辑。剩余工作全部是透传、展示、验证与 Copilot 消费。

---

## T1（P0）：dashboard 透传 business_outcome

**改动点**：`scripts/asa_core/service.py` 的 `dashboard()`，workflows 查询补 `business_outcome` 列（与 `/summary` 同源，直接读 `agent_workflows.business_outcome`）。

**前端**：`src/pages/Overview.tsx` 的工作流条目统一走 `src/workflow/statusMapping.ts` 渲染（AGENTS.md 已有此硬性约定，不得新增本地映射）。若当前总览仍直译 `blocked`，一并收敛。

**验收**：
- [ ] `curl /api/v1/dashboard` 每项 workflow 含 `business_outcome`，且与同 id `/summary` 一致
- [ ] App 总览第 2、3 轮显示"本轮完成，合格人数不足…"而非"已阻塞"
- [ ] Core 测试覆盖 dashboard 新字段；`npm run ci` 全绿

## T2（P0）：工作流详情渲染渠道漏斗 + 0 归因中文解释

**改动点**：`src/workflows/WorkflowPanel.tsx`（或拆出新组件文件，遵守 main.tsx 拆分后的目录结构），调用 `/api/v1/workflows/{id}/sourcing-funnel`。

**展示要求**：
- 每个渠道一行：`查询 N 组 → 召回 X → 抽取 Y → 排重后 Z → 详情（完整 a / 部分 b / 失败 c）→ 入库新增 e（排重命中 d）→ 评估 f（高分 g）`
- `zero_attribution` 非空时显示中文解释，映射表（以后端 7 枚举为准）：
  - `session_expired` → 登录态失效，需重新登录该渠道
  - `loading_incomplete` → 页面加载未完成或查询未生效
  - `page_structure_changed` → 页面结构变化，解析器需要适配
  - `parse_failure` → 平台有结果但解析抓取失败
  - `no_results` → 该渠道真实无匹配结果
  - `unknown` → 原因待排查（同时展示 `error` 摘要）
- 空数据（历史轮次）显示"该轮未记录渠道明细"，不报错不空白
- 样式克制，复用 statusMapping 语义色调；不卡片套卡片
- 新增代码禁止显式 `any`；类型从 `src/generated/api.d.ts` 取

**验收**：
- [ ] Vitest 覆盖：正常漏斗渲染、zero_attribution 六类中文映射、空数据回落
- [ ] 数字守恒断言（测试侧）：`detail_complete + detail_partial + detail_failed ≤ extracted_count`；`intake_new_count ≤ detail_complete`

## T3（P0）：下一轮真实寻访验证漏斗写入

- 用 #154（或当前活跃岗位）跑一轮真实寻访（走正式 R3 审批链路），验证 `agent_sourcing_funnel` 落行：猎聘、X-SaaS 各一行，数值与 audit 日志一致。
- 若 X-SaaS 再次 0 召回，确认 `zero_attribution` 正确分类（这本身就是对既有归因逻辑的首次实战验证）。
- 若执行链路未触发 `_persist_sourcing_funnel`（旧轮次为空属预期，新轮次必须有行），定位并修复写入触发点。

**验收**：
- [ ] 新轮次 `/sourcing-funnel` 返回非空 channels/runs
- [ ] 同步 + A 系统回归守卫 `failure_count: 0`

## T4（P1）：Copilot 消费业务终态与漏斗

- A System Agent 组装 Copilot 上下文时注入：工作流 `business_outcome` 中文语义（复用 `classify_business_outcome` 口径，不新造第三套映射）+ 当轮 `agent_sourcing_funnel` 行。
- 用户问"这轮寻访什么结果 / 为什么没找到人"，回答须与界面终态文案语义一致，可引用漏斗数字；历史无数据轮次明确说"该轮未记录渠道明细"。
- 禁止编造数字；既有"明确写入 vs 询问句"区分不得回归（R12-b 纯转发边界不动）。

**验收**：
- [ ] 实测提问"士兰微第 3 轮寻访什么结果"，回答与 `completed_needs_review` 语义一致，无"执行失败/系统故障"表述
- [ ] 新增 Copilot 回归用例覆盖业务终态询问

## T5（P2）：business_outcome 契约冻结

- 四枚枚举冻结为接口契约：`completed_target_met` / `completed_needs_review` / `completed_pool_insufficient` / `failed_technical`；新增值须同步更新 `workflow.py` 判定、`statusMapping.ts`、契约测试。
- 补契约测试：后端输出仅在枚举集 ∪ {null} 内；前端遇未知值回落 status 原逻辑（`statusMapping.ts` 现有行为，固化成测试）。
- `backfill_business_outcome.py --apply` 前先 dry-run 并把输出贴入 commit message；只 UPDATE `business_outcome` 列。

---

## 全局约束（继承交接文档，强调）

- 候选人真实写入继续走 preflight/commit、幂等、审计、A 系统同步；不新增绕过路径。
- OpenCLI 保持 `read_only_shadow`，不进 funnel 表、不进 intake/触达/写库（R14 趋势聚合器沿用其自有通道）。
- 不改 `status` 状态机语义；`blocked` 保留为技术/流程状态，业务终态只通过 `business_outcome` 表达。
- T1-T5 各自独立 commit、独立过守卫；`npm run ci` + Core 测试 + A 系统回归守卫全绿才算完成。
- 涉及扩展/runner 改动按交接文档第 15 节升版本号并跑 DOM guard。

## 建议执行顺序

T1 → T2 → T3 → T4 → T5。T1/T2 是纯透传与展示，风险最低；T3 依赖一次真实寻访窗口；T4 涉及模型侧，配回归用例一起上；T5 收尾。
