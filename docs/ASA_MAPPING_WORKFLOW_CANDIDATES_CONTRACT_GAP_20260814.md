# Mapping 入库与工作流人选接口合同缺口（2026-08-14）

> 状态：已修复（后端契约与前端展示已同步）
> 发现方：前端即时回读验证

## 结论

Mapping 直挖入库会在同一事务中创建 `job_candidates` 与 `candidate_events.mapping_intake`，但工作流人选接口当前只返回已有评估或 `agent_sourcing_attributions` 的关系。因此，纯 Mapping 新关系已进入主“人选列表”和候选详情，却不会进入来源工作流的“人选结果”区。

这不是前端刷新时机问题。前端若把 Mapping 人选乐观插入工作流名单，会错误呈现为本轮多渠道寻访召回，掩盖来源口径。现已通过独立的 Mapping lineage 进入同一工作流结果接口，但仍保持来源类型隔离。

## 当前证据

- 入库写入：`scripts/a_system_agent/mapping_task.py:intake_candidate()` 写入 `job_candidates`，并写入 `candidate_events.event_type='mapping_intake'`。
- 工作流查询：`scripts/a_system_agent/workflow.py:get_workflow_candidates()` 的筛选条件为：

  ```sql
  WHERE jc.job_id=? AND (a.id IS NOT NULL OR sa.id IS NOT NULL)
  ```

  其中 `a` 是完成的评估，`sa` 是 `agent_sourcing_attributions`。Mapping 入库初始状态不满足任一条件。
- 隔离数据库功能回归已证明新关系会即时进入主“人选列表”，但未将其错误地断言为工作流寻访结果。

## 前端已完成的可证明层（2026-08-14）

- Core 的主候选人列表已返回 `source_type='mapping'`；ASA 现在明确显示“Mapping 直挖”，不再错误兜底为“人才库”。
- Core 候选人详情已通过 `entity_source_links` 返回 `source_system='mapping'` 与公开资料 URL；ASA 将其显示为独立来源证据，并明确不属于猎聘/X-SaaS 查询召回。
- 隔离数据库功能回归已证明：任务卡入库后，无需刷新页面即可在主列表看到 Mapping 渠道，打开详情可看到任务卡入库说明和公开资料链接。
- 当前候选人详情响应已返回 `source_lineage`，包含 Mapping 的 `workflow_id`、`artifact_id`、`candidate_index`、事件 ID 和任务卡标题；工作流人选接口同时返回 `attribution.source_type='mapping'`（或独立 `source_lineage`），前端显示任务卡回执，不反推查询词或轮次。

## 后端决策

采用选项一：Mapping 作为同一工作流下可审计的扩圈来源返回，但 `source_type='mapping'` 独立于 Liepin/X-SaaS 渠道归因。

不能把纯 Mapping 关系伪装为猎聘/X-SaaS 召回，也不能从名称、岗位或任务卡文本推断 query attribution。

## 若选择选项一的验收

1. Mapping 新入库关系携带来源 workflow、artifact 与来源类型，且不污染 Liepin/X-SaaS attribution。
2. `/workflows/{id}/candidates` 明确返回该关系及 `attribution.source_type='mapping'`（或等价结构）。
3. 前端显示“Mapping 直挖入库”，不展示不存在的查询词、轮次或渠道。
4. 关系复用、停止状态和不匹配拒收不误进入结果区。
5. 补 Core 单测与隔离数据库 E2E，覆盖新建、复用、未评估三种情况。

实现验收：候选详情 `source_lineage` 与工作流候选 `source_lineage` 均以数据库事件和 `agent_artifacts` 连接为唯一事实来源；Mapping 未评估关系可在对应 Mapping 工作流结果区出现，但不会被计为渠道 query 召回。专项 Core 测试覆盖分页、Mapping lineage、候选详情和非岗位工作流边界。
