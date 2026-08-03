import { describe, expect, it } from 'vitest'
import { agentConversationReducer, initialAgentConversationState } from '../agent/conversationState'

describe('Agent conversation state machine', () => {
  it('重试同一 request id 时替换原轮消息而不是追加', () => {
    const streaming = agentConversationReducer(initialAgentConversationState, {
      type: 'turn_started', requestId: 'request-1', message: '继续推进', context: { type: 'job', id: 154 }, retry: false,
    })
    const partial = agentConversationReducer(streaming, { type: 'turn_text', requestId: 'request-1', content: '半截回答' })
    const failed = agentConversationReducer(partial, { type: 'turn_failed', requestId: 'request-1', error: '连接中断' })
    const retried = agentConversationReducer(failed, {
      type: 'turn_started', requestId: 'request-1', message: '继续推进', context: { type: 'job', id: 154 }, retry: true,
    })

    expect(retried.phase).toBe('streaming')
    expect(retried.messages).toHaveLength(2)
    expect(retried.messages[1].content).toBe('')
  })

  it('忽略已经不是当前任务的流式事件', () => {
    const streaming = agentConversationReducer(initialAgentConversationState, {
      type: 'turn_started', requestId: 'request-old', message: '旧任务', context: { type: 'page' }, retry: false,
    })
    const switched = agentConversationReducer(streaming, { type: 'task_reset' })
    const stale = agentConversationReducer(switched, { type: 'turn_text', requestId: 'request-old', content: '不应出现' })
    expect(stale.messages).toEqual([])
    expect(stale.phase).toBe('idle')
  })

  it('turn_failed 清空 activeRequestId，迟到事件不再改动状态', () => {
    const streaming = agentConversationReducer(initialAgentConversationState, {
      type: 'turn_started', requestId: 'request-1', message: '继续推进', context: { type: 'job', id: 154 }, retry: false,
    })
    const failed = agentConversationReducer(streaming, { type: 'turn_failed', requestId: 'request-1', error: '模型调用失败' })
    expect(failed.phase).toBe('failed')
    expect(failed.activeRequestId).toBeNull()

    const lateText = agentConversationReducer(failed, { type: 'turn_text', requestId: 'request-1', content: '迟到内容' })
    const lateDone = agentConversationReducer(lateText, {
      type: 'turn_done', requestId: 'request-1', result: { session_id: 'task-1', answer: '迟到答案' },
    })
    expect(lateDone.phase).toBe('failed')
    expect(lateDone.error).toBe('模型调用失败')
    expect(lateDone.messages[1].content).toBe('')
  })
})
