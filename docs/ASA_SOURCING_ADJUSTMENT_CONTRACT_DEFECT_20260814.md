# ASA 寻访调整状态机契约缺陷交接单（2026-08-14）

> 状态：已解决（2026-08-14，Core + WorkflowEngine + ASA Web）
> 发现方：前端契约审计  
> 原则：在后端提供真实闭环前，前端不新增伪造的“应用并生成策略”动作。

> 处理结果：已采用本文第四节状态机；历史问题与建议保留作为审计背景，实际实现和验证证据见第九节。

## 一、结论

当前“停止备注 → 寻访调整 → 下一轮策略”的状态机存在确定性契约缺陷：

1. 前端点击“确认应用”后，Core 只把调整从 `pending` 改成 `applied`。
2. 策略生成只读取 `status='pending'` 的调整。
3. 因此，手动确认会让该调整在真正生成下一轮策略前退出学习输入。
4. 该接口没有生成策略、修订现有策略或创建工作流，却向用户返回“已应用”。

结果是：界面显示“已应用”，但调整并未被任何策略消费，也没有 `applied_round` 和有效的策略版本证据。该问题不能通过前端改文案或乐观更新解决。

此外，策略生成成功后的消费逻辑按 `job_id + status='pending'` 批量更新，可能把策略生成期间新写入、实际未进入本次输入的调整一并标为 `applied`。

## 二、可复现调用链

### 2.1 手动确认造成学习输入丢失

1. `asa-web/src/panels/SourcingAdjustments.tsx:101-103`
   - 用户点击“确认应用”。
2. `asa-web/src/api.ts:896-897`
   - 前端调用 `POST /api/v1/sourcing-adjustments/{id}/confirm`。
3. `scripts/asa_core/app.py:1313-1317`
   - 路由转发到 `CoreService.confirm_sourcing_adjustment()`。
4. `scripts/asa_core/service.py:2920-2935`
   - 唯一业务写入是 `pending → applied` 和设置 `applied_at`；没有生成或修订策略。
5. `scripts/a_system_agent/capability_runtime.py:3156-3171`
   - `_strategy_learning_context()` 只查询 `status='pending'`。
6. 下一次 `run_search_strategy()` 执行时，已手动确认的调整不会进入 `stop_note_adjustments`，也不会进入 LLM 的 `stop_note_adjustments_summary`。

### 2.2 批量消费可能错误归因

1. `run_search_strategy()` 在生成前读取一份 `pending` 调整快照。
2. 模型生成和策略校验存在时间窗口。
3. `scripts/a_system_agent/capability_runtime.py:3233-3240` 在生成成功后执行：

   ```sql
   UPDATE agent_sourcing_adjustments
      SET status='applied', ...
    WHERE job_id=? AND status='pending'
   ```

4. 若时间窗口内新增另一条 `pending` 调整，该调整未参与本次策略输入，却会被一并标记为已应用并获得同一 `applied_round`。

## 三、现有测试为何未拦截

- `tests/test_strategy_v2_s4.py:708-741` 只验证 `pending` 可以被学习上下文读取，没有覆盖手动确认后的状态。
- `asa-web/src/__tests__/sourcing-adjustments.test.tsx:53-70` mock 了 `confirm → applied`，只验证请求和界面刷新，等同固化了错误契约。
- 当前没有端到端测试证明“用户确认的调整 ID 出现在下一版策略输入，并在成功产出后才变为 applied”。
- 当前没有并发/快照测试证明生成期间新增的调整不会被错误消费。

## 四、推荐状态机

建议明确区分“顾问已采纳”和“策略已消费”：

| 状态 | 含义 | 是否进入下一次策略输入 | 终态 |
|---|---|---:|---:|
| `pending` | 系统从停止备注提取，等待顾问判断 | 否 | 否 |
| `accepted` | 顾问已采纳，等待下一次策略生成 | 是 | 否 |
| `applied` | 已被某次成功策略明确消费 | 否 | 是 |
| `ignored` | 顾问明确忽略 | 否 | 是 |

状态迁移：

```text
pending --confirm--> accepted --successful strategy consumption--> applied
   |                    |
   +----ignore--------> ignored
                        +----strategy failure----> accepted
```

选择该模型的原因：

- 与现有“确认应用 / 忽略”人工判断界面一致。
- `applied` 可以继续保持“有 `applied_round`、`applied_at`、`baseline_json`，且能指向真实策略产物”的强语义。
- 策略失败时不会丢失顾问已采纳的调整。
- 后续可准确衡量“被采纳但尚未执行”和“执行后效果”。

若产品选择“默认自动应用、顾问仅可否决”，也必须形成另一套一致契约：新调整直接进入 `accepted`，前端移除“确认应用”，仅保留忽略/撤销；不能继续让 `pending` 同时表示“待人工确认”和“无需确认即可被消费”。

## 五、后端实施建议

1. 扩展状态机，`confirm_sourcing_adjustment()` 改为 `pending → accepted`，不要写 `applied_at`。
2. `_strategy_learning_context()` 只读取 `accepted`；若需要兼容升级前数据，迁移时明确处理旧 `pending`，不要长期使用含糊的 `IN ('pending','accepted')`。
3. 在生成前固定本次消费的 adjustment ID 集合，并把 IDs 随运行上下文传到消费步骤。
4. `_apply_stop_note_adjustments()` 仅更新这组 IDs，且限定 `status='accepted'`；禁止再按岗位更新所有待处理记录。
5. 仅在 `strategy_v2` 校验成功、策略产物已持久化（或同一事务可保证持久化）后写 `applied`、`applied_at`、`applied_round`、`baseline_json`。
6. 增加可审计关联。优先新增 `applied_workflow_id` 和 `applied_artifact_id`（或独立关联表），让“已应用”能追溯到具体策略版本。
7. API 返回完整调整对象或至少返回 `status='accepted'`；同步 OpenAPI 后重新生成 `asa-web/src/generated/api.d.ts`。
8. 对现有数据做一次审计：重点查找 `status='applied' AND applied_round IS NULL`。这类记录大概率来自手动确认，不能假定已进入策略。

建议审计 SQL（只读执行）：

```sql
SELECT id, job_id, candidate_id, adjust_type, value, applied_at
FROM agent_sourcing_adjustments
WHERE status = 'applied'
  AND applied_round IS NULL
ORDER BY id;
```

迁移旧记录时需结合工作流/策略产物证据判断。没有证据的记录应回到 `accepted`，而不是补造 `applied_round`。

## 六、必须补齐的测试

1. `confirm`：`pending → accepted`，不设置 `applied_at/applied_round/baseline_json`。
2. 学习输入：`accepted` 被读取；`pending/applied/ignored` 不被读取。
3. 成功消费：本次输入中的 IDs 变为 `applied`，并写入轮次、基线和策略关联。
4. 失败保留：LLM 异常、schema 校验失败或产物持久化失败时，调整仍为 `accepted`。
5. 快照隔离：策略生成期间新增的 `accepted` 调整不属于本轮，不得被标记为 `applied`。
6. 岗位隔离：其他岗位调整不受影响。
7. 幂等：同一幂等键重放返回首次结果；不同请求重复确认已接受记录时，返回稳定且可理解的结果。
8. API 契约：OpenAPI 枚举和响应包含 `accepted`。
9. 前后端功能 E2E：确认后显示“已采纳，待下轮策略”；真实策略成功后才显示“已应用于第 N 轮”。

## 七、前端后续配合

后端契约完成并提供可测试 Core 后，前端再做以下改动：

1. `SourcingAdjustmentStatus` 增加 `accepted`。
2. “确认应用”改为更准确的“采纳调整”。
3. 列表增加“已采纳，待下轮策略”分组；只有 `applied` 进入“已应用”历史。
4. `applied` 行展示具体轮次及可追溯策略版本。
5. 更新 `sourcing-adjustments.test.tsx`，删除 `confirm → applied` 的错误 mock 契约。
6. 运行 `npm run ci:fast`、`npm run ci:e2e-functional`、`npm run test:contract`。

## 八、验收标准

以 adjustment ID 为审计主线，必须能证明：

1. 顾问点击采纳后，该 ID 仍处于可被下一轮策略读取的非终态。
2. 下一轮策略的生成输入或审计 trace 明确包含该 ID 及其值。
3. 只有策略成功产出后，该 ID 才进入 `applied`。
4. `applied` 记录可以追溯到 workflow、轮次和策略 artifact/version。
5. 策略失败或并发新增调整时，不发生丢失和错误归因。

在以上条件满足前，不能把当前 `POST .../confirm` 的成功响应视为“学习闭环已完成”。

## 九、解决结果与验证证据（2026-08-14）

### 9.1 已落地合同

1. Core migration 11 新增 `accepted_at`、`applied_workflow_id`、`applied_artifact_id` 并已部署到正式 Core。迁移前 7 条 `applied` 记录虽都有轮次与基线，但没有可证明的 workflow/artifact 关联，继续作为明确的历史记录保留，不补造 lineage；新合同从 migration 11 起强制执行。
2. `confirm_sourcing_adjustment()` 只执行 `pending → accepted`，不写 `applied_at`、轮次、基线或策略引用；不同请求重复采纳同一 `accepted` 记录返回稳定的 `already_accepted` 回执。
3. 策略学习只读取 `accepted`。`run_search_strategy()` 在生成前固定 adjustment ID 与值，并把输入快照写入策略 artifact metadata。
4. WorkflowEngine 在 `strategy_v2` 校验通过、`search_strategy` artifact 插入成功后，在同一 SQLite 事务内按固定 ID 集合更新 `accepted → applied`；写入轮次、候选池基线、workflow ID 和 artifact ID，并记录 `sourcing_adjustments_applied` 事件。
5. 生成期间新增的 `accepted` 调整不在固定集合内，保持 `accepted` 等待下一轮；schema 校验失败时同样不消费。
6. OpenAPI 已提供结构化列表/决策响应和 `pending | accepted | applied | ignored` 枚举，前端生成类型已同步。
7. ASA 将动作改为“采纳调整”，单独展示“已采纳，待下轮策略”；只有真实 `applied` 才进入历史并显示轮次、workflow 与策略 artifact。

### 9.2 验证

- 后端相关回归：30 项通过（状态机、OpenAPI 枚举、完整 strategy_v2、失败保留、快照隔离、migration 幂等）。
- 前端 L1：68 个测试文件、613 项测试通过；typecheck、build、API drift 通过；lint 0 error / 8 条既有 warning。
- Python 源码契约：58/58 通过。
- 隔离数据库功能 E2E：27/27 通过；新增用例真实确认 `pending → accepted`，并验证策略未产出前没有应用轮次或 lineage；隔离 Core 和临时 DB 均已清理。
- 正式 Core 已通过 LaunchAgent 重启到新代码，health 正常，OpenAPI 返回四态枚举；migration 11 生效后业务状态仍为 7 条历史 `applied`、1 条 `pending`。A 系统 regression guard `failure_count=0`。

### 9.3 审计主线

对 migration 11 后新产生的任一已应用 adjustment，可以沿以下字段闭环核验：

```text
agent_sourcing_adjustments.id
  → strategy artifact metadata.sourcing_adjustment_input[].id/value
  → applied_workflow_id
  → applied_artifact_id
  → applied_round / applied_at / baseline_json
  → agent_step_events.sourcing_adjustments_applied
```
