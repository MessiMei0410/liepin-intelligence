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
  | { type: 'restore_failed'; error: string }
  | { type: 'turn_started'; requestId: string; message: string; context: AgentContext; retry: boolean }
  | { type: 'turn_text'; requestId: string; content: string }
  | { type: 'turn_done'; requestId: string; result: AgentTurnResult }
  | { type: 'turn_failed'; requestId: string; error: string }
  | { type: 'turn_stopped'; requestId: string }
  | { type: 'card_refreshed'; jobId: number; content: string; action_card: Record<string, unknown> | null }

export const agentConversationReducer = (
  state: AgentConversationState,
  action: AgentConversationAction,
): AgentConversationState => {
  if (action.type === 'task_reset') return initialAgentConversationState
  if (action.type === 'restore_started') return { ...initialAgentConversationState, phase: 'restoring' }
  if (action.type === 'restore_succeeded') return { ...initialAgentConversationState, messages: action.messages }
  if (action.type === 'restore_failed') return { ...initialAgentConversationState, phase: 'failed', error: action.error }
  if (action.type === 'turn_started') {
    const retained = action.retry
      ? state.messages.filter(message => message.turnRequestId !== action.requestId)
      : state.messages
    return {
      phase: 'streaming', activeRequestId: action.requestId, error: '',
      messages: [
        ...retained,
        { role: 'user', content: action.message, context: action.context, turnRequestId: action.requestId },
        { role: 'assistant', content: '', turnRequestId: action.requestId },
      ],
    }
  }
  // 名单卡刷新：按 jobId 更新所有 candidate_list 消息的 action_card + 回答文本（不依赖 requestId）
  if (action.type === 'card_refreshed') return {
    ...state,
    messages: state.messages.map(message => {
      const card = message.action_card && typeof message.action_card === 'object' ? message.action_card as { type?: unknown; context?: { type?: unknown; id?: unknown } } : null
      const isTarget = Boolean(
        message.role === 'assistant'
        && card?.type === 'candidate_list'
        && card.context?.type === 'job'
        && Number(card.context.id) === action.jobId,
      )
      return isTarget
        ? { ...message, content: action.content, action_card: action.action_card }
        : message
    }),
  }
  if (action.requestId !== state.activeRequestId) return state
  if (action.type === 'turn_text') return {
    ...state,
    messages: state.messages.map(message => message.role === 'assistant' && message.turnRequestId === action.requestId
      ? { ...message, content: message.content + action.content }
      : message),
  }
  if (action.type === 'turn_done') return {
    ...state, phase: 'idle', activeRequestId: null, error: '',
    messages: state.messages.map(message => message.role === 'assistant' && message.turnRequestId === action.requestId
      ? {
          ...message, content: action.result.answer || message.content, context: action.result.context,
          references: action.result.references, suggested_actions: action.result.suggested_actions,
          business_focus: action.result.business_focus, workflow_progress: action.result.workflow_progress,
          workflow_id: action.result.workflow_id, pending_intent: action.result.pending_intent,
          action_card: action.result.action_card, model_participation: action.result.model_participation,
        }
      : message),
  }
  if (action.type === 'turn_failed') return { ...state, phase: 'failed', activeRequestId: null, error: action.error }
  return { ...state, phase: 'stopped', activeRequestId: null, error: '' }
}
