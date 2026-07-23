# ASA 寻访漏斗指标口径（PRD R8）

日期：2026-07-22
适用范围：工作流详情「渠道漏斗」区块、`GET /api/v1/workflows/{id}/sourcing-funnel` 接口、`agent_sourcing_funnel` 表。

## 1. 数据流

```
猎聘/X-SaaS runner（rounds 明细 + detail_capture 三态）
  → capability_runtime._persist_sourcing_funnel（执行落库时同步写，失败不阻断主链路）
  → agent_sourcing_funnel 表（run_id × channel 唯一，重跑更新不追加歧义行）
  → asa_core.workflow_sourcing_funnel 聚合（channels 渠道级合计 + runs run 级明细）
  → GET /api/v1/workflows/{id}/sourcing-funnel
  → 前端 WorkflowFunnel 组件（src/workflows/WorkflowFunnel.tsx）
```

- 0 结果归因由 `capability_runtime.classify_zero_result` 用 runner 输出的既有信号（error 文本、
  per-query rounds 的 status/reason/result_count/extracted_count）判定，前端不做猜测。
- OpenCLI 只读影子链路**不写入**漏斗表、不参与 intake/触达/写库（`read_only_shadow` 契约不变）。
- 历史轮次（漏斗表无行）接口返回空 `channels/runs`，前端显示「该轮未记录渠道明细」，不报错不空白。

## 2. 指标口径

渠道级聚合字段（同一工作流同一渠道多条 run 时求和）：

| 指标 | 字段 | 口径 |
| --- | --- | --- |
| 查询组数 | `runs[].query_count`（渠道级为各 run 合计） | 本轮该渠道执行的关键词/查询组数 |
| 召回数 | `recall_count` | 各组查询平台侧结果数（`result_count`）之和；列表召回，不等于抓取成功 |
| 抽取数 | `extracted_count` | 列表页实际抽取的候选卡片数（各组 `extracted_count` 之和；无 rounds 明细时=渠道唯一候选数） |
| 渠道内排重 | `dedupe_count` | `max(0, extracted_count − unique_count)`，渠道内去重掉的重合卡片 |
| 排重后候选 | `unique_count` | 渠道内排重后的唯一候选人数 |
| 详情 完整/部分/失败 | `detail.complete / partial / failed` | 详情页抓取 `resume_capture_status` 三态分布；只有 complete 允许进入正式 intake |
| 完整率 | `detail.complete_rate` | `complete / (complete+partial+failed)`，三态全 0 时为 null |
| 入库排重命中 | `intake_duplicate_count` | intake 阶段命中已有候选人/批内重复的条数（`existing + batch_duplicates` 按渠道归集） |
| 入库新增 | `intake_new_count` | 本轮该渠道实际新增入库人数（`attributions.channel_new`） |
| 评估数 | `assessed_count` | 获得 ASA 评分（`fit_score` 非空）的候选人数 |
| 高分（评估通过） | `high_score_count` | `fit_score ≥ 65` 的候选人数（与执行器 `--recommend-score 65` 口径一致） |

守恒约束（测试侧断言）：

- `detail.complete + detail.partial + detail.failed ≤ extracted_count`（详情抓取只在抽取到的卡片上进行）
- `intake_new_count ≤ detail.complete`（只有完整简历允许入库）

查询明细（`runs[].queries`）：每组查询保留 runner 原始记录，常用键为
`query`（查询文本）、`result_count`（平台结果数）、`extracted_count`（抽取数）、
`status` / `reason`（异常标记，如 `stale_query`、`search_controls_missing`）。随 runner 演进保持开放，前端宽松读取。

## 3. 0 结果归因枚举（`zero_attribution`）

当渠道本轮候选数为 0 时填写；非 0 结果渠道为 null。判定来源全部为 runner 输出的既有信号：

| 枚举 | 中文展示 | 判定信号 |
| --- | --- | --- |
| `no_results` | 真实无结果：该渠道无匹配人选 | 各组查询平台返回 0 条，或抓到后全部被评分门槛/排重过滤 |
| `session_expired` | 登录态失效，需重新登录该渠道 | error 含 `LOGIN_REQUIRED` / 「登录已过期」/「登录态失效」 |
| `loading_incomplete` | 页面加载未完成或查询未生效 | error 含「加载超时/未加载」，或 rounds 出现 `stale_query` |
| `page_structure_changed` | 页面结构变化，解析器需要适配 | rounds 出现 `search_controls_missing` |
| `parse_failure` | 平台有结果但解析抓取失败 | 平台结果数 > 0 但抽取数为 0 |
| `unknown` | 质量未知，原因待排查 | 以上信号均不足；前端同时展示 `error` 摘要 |

前端纪律：

- 0 召回/0 入库的渠道必须展示归因中文解释（步骤业务结果与渠道漏斗两处），
  **禁止把 0 结果默认显示为 `completed` 成功**。
- 渠道执行失败时 `_record_sourcing_funnel_failure` 尽力留一行 `status=failed` + 归因 + error，不掩盖原始异常。
- 归因未知新值前端回落「待排查（原值）」，不渲染裸英文枚举。

## 4. 轮询负载说明（R7 纪律延续）

- 漏斗走**独立按需路由** `/sourcing-funnel`，不进入 `/summary` 轮询签名（`summarySignature` 不变）。
- 前端仅在工作流面板挂载与完整详情刷新（`updated_at` 变化）时拉取一次；活跃轮询仍只打小 payload 的 `/summary`。
- 步骤完整 output（含 audit stdout）继续走 `/steps/{id}` 按需加载，漏斗接口本身只含聚合数字与查询组文本。

## 5. 验证方式

- 前端：`src/__tests__/workflow-funnel.test.tsx`（正常渲染 / 六类归因映射 / 空数据回落 / 守恒断言 / 失败降级 / 步骤结果 0 结果文案）。
- 后端：`liepin-intelligence/tests/test_sourcing_funnel.py`（建表幂等、唯一约束、聚合接口、归因分类）。
- 真实巡检：打开任一包含「执行多渠道寻访」步骤的工作流详情，「渠道漏斗」区块在「寻访策略」与「人选结果」之间；
  新跑一轮真实寻访后应有猎聘、X-SaaS 各一条渠道行。
