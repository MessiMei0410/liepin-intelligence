# ASA AgentOS 行动卡闭环

## 目标

ASA 的可执行建议必须走同一条可审计链路：上下文确认、行动卡、预检、单次确认、执行、回查和审计。Copilot 只解释和呈现卡片，不绕过既有候选人确认或 R3 工作流审批。

## Capability Manifest v2

`CapabilitySpec.public()` 是能力清单事实源。每项能力必须声明：

- `action_kind`: `read`、`draft`、`internal_write` 或 `external_write`。
- `preflight_mode`: `none`、`preview` 或 `required`。
- `confirmation_surface`: `none`、`floating_card` 或 `workflow_approval`。
- `post_check`: `none`、`result` 或 `external_evidence`。
- `audit_event_type`: 统一审计事件类型。

未注册能力不能被执行器选择；R2/R3 能力没有单次授权不得运行；Browser Adapter 对 R2/R3 额外校验 `_approval_granted`。

## 行动卡协议

所有 Agent proposal 和 Copilot 候选人确认意图以 `action_card` 表示。最小字段为：

```json
{
  "proposal_id": "proposal_xxx 或 null",
  "capability_id": "verification_plan",
  "action_kind": "internal_write",
  "risk_level": "R1",
  "context": {"type": "candidate", "id": 123},
  "evidence": [{"label": "建议原因", "value": "..."}],
  "blocked_reasons": [],
  "next_actions": [{"type": "preflight", "label": "查看预检"}],
  "post_check": "agent_action"
}
```

`proposal_id=null` 仅用于候选人状态动作。它明确表示该卡继续使用既有 `candidate-actions/preflight` 与 `candidate-actions/commit` 令牌链路，不创建平行 proposal 或跳过审计。

## 执行边界

- 内部 `create_task`：仅在 proposal 预检和确认后执行；执行写入 `agent_actions`，终态写回 `agent_action_proposals`，并返回 `post_check`。
- 候选人动作：浮窗确认 `pending_intent` 后调用 Core v1 intent-confirm；令牌一次性使用，状态漂移返回 409。
- workflow 与 R3 外部动作：行动卡只能建立或打开真实 workflow。只有返回真实 `workflow_id` 后，Copilot 才能说已开始；外部寻访、发布和触达仍由 `agent_approvals` 单次审批。
- Agent mode：`/api/v1/copilot/agent` 也经过 Core 行动卡仲裁。没有真实 `workflow_id` 时过滤可执行建议，并纠正“寻访已启动”等无依据表述；有真实 workflow 时补齐行动卡、审批入口和会话持久化，同时保留策略补丁等既有结构化字段。
- 未知或外部 proposal action：失败关闭，不允许由行动卡自动执行。

## UI 归属

原生 ASA Copilot 浮窗是唯一交互面。浮窗显示对象、风险、证据、预检结果、确认状态和回查回执；提案执行后可直接打开对应候选人，workflow 卡可打开真实执行计划。React App 只发布候选人名、客户、岗位、workflow mode 和当前页面等稳定上下文、唤起浮窗，并在总览提供 Agent 行动卡入口。`?surface=copilot` 不恢复对话 UI，也不经 URL 传递上下文。

## 七天指标

`GET /api/v1/agent/metrics?days=7` 提供只读复盘数据：

- `action_cards_generated`: 期间创建的 proposal 卡数。
- `confirmation_rate`: 已确认 proposal / 已创建 proposal。
- `rejection_rate`: 已拒绝 proposal / 已创建 proposal。
- `execution_failure_rate`: 失败 /（已执行 + 失败）。
- `needs_clarification`: Copilot 要求澄清的持久化会话数。
- `r3_approvals`: R3 审批总数、批准数与批准率。

分母为零时比率返回 `null`，不伪造 0%。总览轻量展示待确认、已执行、失败三种 proposal 状态；详细复盘从 Core 指标接口读取。

## 接入检查

新能力接入前必须证明：Manifest v2 字段完整、行动卡包含上下文/证据/风险/下一步、写入有预检和一次性确认、执行后有 post-check 与审计记录，并覆盖 Core 契约测试和浮窗交互测试。
