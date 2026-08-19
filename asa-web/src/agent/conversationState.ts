import type { AgentMessage } from './sessionModel'
import type { AgentContext, AgentSubagentRun, AgentTurnResult } from './transport'

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
  | { type: 'turn_thinking'; requestId: string; content: string }
  | { type: 'turn_subagent'; requestId: string; run: AgentSubagentRun }
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
  // 名单卡也可能只在复数卡 action_cards 里（DSH 委托轮无单卡 action_card），同样按引用定位。
  if (action.type === 'card_refreshed') return {
    ...state,
    messages: state.messages.map(message => {
      if (message.role !== 'assistant') return message
      const directHit = message.action_card === action.sourceCard
      const arrayHit = Array.isArray(message.action_cards) && message.action_cards.some(card => card === action.sourceCard)
      if (!directHit && !arrayHit) return message
      return {
        ...message,
        content: action.content,
        action_card: directHit || !message.action_card ? action.action_card : message.action_card,
        action_cards: arrayHit && Array.isArray(message.action_cards)
          ? message.action_cards.map(card => (card === action.sourceCard ? action.action_card : card)).filter((card): card is Record<string, unknown> => Boolean(card))
          : message.action_cards,
      }
    }),
  }
  if (action.requestId !== state.activeRequestId) return state
  if (action.type === 'turn_text') return {
    ...state,
    messages: state.messages.map(message => message.role === 'assistant' && message.turnRequestId === action.requestId
      ? { ...message, content: message.content + action.content }
      : message),
  }
  // DSH 思考过程（reasoning-delta → thinking 事件）：增量挂到本轮 assistant 消息，
  // 与正文分通道渲染（折叠区），不进 content、不进 markdown 重解析链路。
  if (action.type === 'turn_thinking') return {
    ...state,
    messages: state.messages.map(message => message.role === 'assistant' && message.turnRequestId === action.requestId
      ? { ...message, thinking: (message.thinking || '') + action.content }
      : message),
  }
  // DSH 子代理运行（SSE subagent 事件）：按 run.id 归并到本轮 assistant 消息的
  // subagents 数组，流式更新卡片状态（running→done/failed/stopped）。
  if (action.type === 'turn_subagent') return {
    ...state,
    messages: state.messages.map(message => {
      if (message.role !== 'assistant' || message.turnRequestId !== action.requestId) return message
      const existing = message.subagents || []
      const index = existing.findIndex(run => run.id === action.run.id)
      // end 增量不带 label（asa-server 只在 start 发）：归并时保留 start 的描述。
      const subagents = index >= 0
        ? existing.map((run, i) => (i === index ? { ...run, ...action.run, label: action.run.label || run.label } : run))
        : [...existing, action.run]
      return { ...message, subagents }
    }),
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
          analysis_card: action.result.analysis_card, business_focus: action.result.business_focus,
          workflow_progress: action.result.workflow_progress,
          workflow_id: action.result.workflow_id, pending_intent: action.result.pending_intent,
          action_card: action.result.action_card, confirm_request: action.result.confirm_request,
          // 子代理终态快照：done 携带时覆盖流式聚合（含轮末仍 running 的后台委派）。
          subagents: action.result.subagents || message.subagents,
          // 复数卡片随轮保存：DSH 委托轮 done 只带 action_cards 不带 action_card，
          // 常驻「查看名单」按钮/自动弹窗/引用抑制都要能从数组里找到 candidate_list。
          action_cards: action.result.action_cards,
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
