import type { AgentMessage } from './sessionModel'
import type { AgentContext, AgentTurnResult } from './transport'

type ConversationMessage = AgentMessage & { turnRequestId?: string }

export type AgentConversationState = {
  phase: 'idle' | 'restoring' | 'streaming' | 'failed' | 'stopped'
  messages: ConversationMessage[]
  activeRequestId: string | null
  error: string
}

export const initialAgentConversationState: AgentConversationState = {
  phase: 'idle', messages: [], activeRequestId: null, error: '',
}

export type AgentConversationAction =
  | { type: 'task_reset' }
  | { type: 'restore_started' }
  | { type: 'restore_succeeded'; messages: AgentMessage[] }
  | { type: 'history_prepended'; messages: AgentMessage[] }
  | { type: 'restore_failed'; error: string }
  | { type: 'turn_started'; requestId: string; message: string; context: AgentContext; retry: boolean; continuation?: boolean }
  | { type: 'turn_text'; requestId: string; content: string }
  | { type: 'turn_done'; requestId: string; result: AgentTurnResult }
  | { type: 'turn_failed'; requestId: string; error: string }
  | { type: 'turn_stopped'; requestId: string }
  | { type: 'card_refreshed'; sourceCard: Record<string, unknown>; content: string; action_card: Record<string, unknown> | null }

export const agentConversationReducer = (
  state: AgentConversationState,
  action: AgentConversationAction,
): AgentConversationState => {
  if (action.type === 'task_reset') return initialAgentConversationState
  if (action.type === 'restore_started') return { ...initialAgentConversationState, phase: 'restoring' }
  if (action.type === 'restore_succeeded') return { ...initialAgentConversationState, messages: action.messages }
  // 「加载更早」：早页消息插到最前，phase/activeRequestId 原样保留——
  // 进行中的流式轮次（streaming）不受影响，避免打断生成。
  if (action.type === 'history_prepended') return { ...state, messages: [...action.messages, ...state.messages] }
  if (action.type === 'restore_failed') return { ...initialAgentConversationState, phase: 'failed', error: action.error }
  if (action.type === 'turn_started') {
    const retained = action.retry
      ? state.messages.filter(message => message.turnRequestId !== action.requestId)
      : state.messages
    return {
      phase: 'streaming', activeRequestId: action.requestId, error: '',
      messages: action.continuation
        ? [...retained, { role: 'assistant', content: '', context: action.context, turnRequestId: action.requestId }]
        : [
            ...retained,
            { role: 'user', content: action.message, context: action.context, turnRequestId: action.requestId },
            { role: 'assistant', content: '', turnRequestId: action.requestId },
          ],
    }
  }
  // 名单卡刷新：只更新被点击的那条消息——同一岗位可能同时存在普通名单卡和严格筛选
  // 名单卡，按 jobId 全覆盖会把另一张卡的内容也替换掉。以发起刷新时的卡片对象引用
  // （sourceCard）定位消息；消息流与弹窗始终持有同一卡片对象，引用匹配是精确的。
  if (action.type === 'card_refreshed') return {
    ...state,
    messages: state.messages.map(message =>
      message.role === 'assistant' && message.action_card === action.sourceCard
        ? { ...message, content: action.content, action_card: action.action_card }
        : message
    ),
  }
  if (action.requestId !== state.activeRequestId) return state
  if (action.type === 'turn_text') return {
    ...state,
    messages: state.messages.map(message => message.role === 'assistant' && message.turnRequestId === action.requestId
      ? { ...message, content: message.content + action.content }
      : message),
  }
  if (action.type === 'turn_done') {
    const revokedIds = new Set(
      (action.result.revoked_actions || []).flatMap(item => [
        item.action_id,
        ...(Array.isArray(item.action_ids) ? item.action_ids : []),
      ]).map(value => String(value || '')).filter(Boolean),
    )
    const revokedPendingActions = new Set(
      (action.result.revoked_actions || []).map(item => String(item.action || '')).filter(Boolean),
    )
    const revokedWorkflowIds = new Set(
      (action.result.revoked_actions || []).map(item => String(item.workflow_id || '')).filter(Boolean),
    )
    return {
      ...state, phase: 'idle', activeRequestId: null, error: '',
      messages: state.messages.map(message => {
        const actionIds = (message.suggested_actions || []).map(item => String(item.action_id || '')).filter(Boolean)
        const pendingAction = message.pending_intent && typeof message.pending_intent === 'object'
          ? String(message.pending_intent.action || '')
          : ''
        const actionCard = message.action_card && typeof message.action_card === 'object' ? message.action_card : undefined
        const actionCardContext = actionCard?.context && typeof actionCard.context === 'object'
          ? actionCard.context as Record<string, unknown>
          : undefined
        const workflowId = String(message.workflow_id || actionCardContext?.id || '')
        const revoked = actionIds.some(id => revokedIds.has(id))
          || Boolean(pendingAction && revokedPendingActions.has(pendingAction))
          || Boolean(workflowId && revokedWorkflowIds.has(workflowId))
        if (revoked) return {
          ...message,
          invalidated: true,
          invalidated_reason: '本轮纠正已撤销依赖旧条件的待执行动作',
        }
        return message.role === 'assistant' && message.turnRequestId === action.requestId
          ? {
          ...message, content: action.result.answer || message.content, context: action.result.context,
          references: action.result.references, suggested_actions: action.result.suggested_actions,
          understanding_card: action.result.understanding_card, execution_receipt: action.result.execution_receipt,
          business_focus: action.result.business_focus, workflow_progress: action.result.workflow_progress,
          workflow_id: action.result.workflow_id, pending_intent: action.result.pending_intent,
          action_card: action.result.action_card, confirm_request: action.result.confirm_request,
          model_participation: action.result.model_participation,
          strategy_patch: action.result.strategy_patch,
          strategy_patch_applied: action.result.strategy_patch_applied,
          strategy_patch_ignored: action.result.strategy_patch_ignored,
          strategy_patch_revision: action.result.strategy_patch_revision,
          strategy_patch_artifact_id: action.result.strategy_patch_artifact_id,
          strategy_patch_applied_count: action.result.strategy_patch_applied_count,
          invalidated: action.result.invalidated, invalidated_reason: action.result.invalidated_reason,
          revoked_actions: action.result.revoked_actions,
            }
          : message
      }),
    }
  }
  if (action.type === 'turn_failed') return { ...state, phase: 'failed', activeRequestId: null, error: action.error }
  return { ...state, phase: 'stopped', activeRequestId: null, error: '' }
}
