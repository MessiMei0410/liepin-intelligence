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

  it('turn_thinking 增量挂到本轮 assistant 消息，与正文分通道、turn_done 后保留', () => {
    const streaming = agentConversationReducer(initialAgentConversationState, {
      type: 'turn_started', requestId: 'request-1', message: '分析一下', context: { type: 'page' }, retry: false,
    })
    const t1 = agentConversationReducer(streaming, { type: 'turn_thinking', requestId: 'request-1', content: '先看岗位' })
    const t2 = agentConversationReducer(t1, { type: 'turn_thinking', requestId: 'request-1', content: '，再看人选' })
    const withText = agentConversationReducer(t2, { type: 'turn_text', requestId: 'request-1', content: '结论如下' })
    expect(withText.messages[1].thinking).toBe('先看岗位，再看人选')
    expect(withText.messages[1].content).toBe('结论如下')

    // 非本轮 request 的 thinking 被忽略
    const foreign = agentConversationReducer(withText, { type: 'turn_thinking', requestId: 'request-x', content: '不该出现' })
    expect(foreign.messages[1].thinking).toBe('先看岗位，再看人选')

    const done = agentConversationReducer(withText, {
      type: 'turn_done', requestId: 'request-1', result: {
        ok: true, session_id: 'task-1', answer: '结论如下',
        analysis_card: { headline: '分析结论' },
      },
    })
    expect(done.messages[1].thinking).toBe('先看岗位，再看人选')
    expect(done.messages[1].analysis_card).toEqual({ headline: '分析结论' })
    expect(done.messages[1].content).toBe('结论如下')
  })

  it('turn_subagent 按 run.id 归并流式更新；turn_done 的 subagents 快照覆盖流式聚合', () => {
    const streaming = agentConversationReducer(initialAgentConversationState, {
      type: 'turn_started', requestId: 'request-1', message: '背调这两个人', context: { type: 'page' }, retry: false,
    })
    const s1 = agentConversationReducer(streaming, {
      type: 'turn_subagent', requestId: 'request-1', run: { id: 'run-1', label: '背调甲', status: 'running' },
    })
    const s2 = agentConversationReducer(s1, {
      type: 'turn_subagent', requestId: 'request-1', run: { id: 'run-2', label: '背调乙', status: 'running' },
    })
    expect(s2.messages[1].subagents).toEqual([
      { id: 'run-1', label: '背调甲', status: 'running' },
      { id: 'run-2', label: '背调乙', status: 'running' },
    ])
    // end 事件按 id 归并更新（label 缺省时保留 start 的 label）
    const s3 = agentConversationReducer(s2, {
      type: 'turn_subagent', requestId: 'request-1', run: { id: 'run-1', label: '', status: 'done', summary: '甲已核实' },
    })
    expect(s3.messages[1].subagents?.[0]).toEqual({ id: 'run-1', label: '背调甲', status: 'done', summary: '甲已核实' })

    // 非本轮 request 的 subagent 事件被忽略
    const foreign = agentConversationReducer(s3, {
      type: 'turn_subagent', requestId: 'request-x', run: { id: 'run-9', label: '不该出现', status: 'running' },
    })
    expect(foreign.messages[1].subagents).toHaveLength(2)

    // done 快照（run-1 完成、run-2 仍 running）覆盖流式聚合
    const done = agentConversationReducer(s3, {
      type: 'turn_done', requestId: 'request-1', result: {
        ok: true, session_id: 'task-1', answer: '背调结果如下',
        subagents: [
          { id: 'run-1', label: '背调甲', status: 'done', summary: '甲已核实' },
          { id: 'run-2', label: '背调乙', status: 'running' },
        ],
      },
    })
    expect(done.messages[1].subagents).toEqual([
      { id: 'run-1', label: '背调甲', status: 'done', summary: '甲已核实' },
      { id: 'run-2', label: '背调乙', status: 'running' },
    ])
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

  it('纠正回合按 revoked action ids 立即失效历史行动卡', () => {
    const prior = {
      ...initialAgentConversationState,
      phase: 'idle' as const,
      messages: [{
        role: 'assistant' as const,
        content: '旧计划',
        suggested_actions: [{ action_id: 'action-old', type: 'continue_sourcing' }],
      }],
    }
    const streaming = agentConversationReducer(prior, {
      type: 'turn_started', requestId: 'request-correction', message: '刚才不对', context: { type: 'job', id: 154 }, retry: false,
    })
    const completed = agentConversationReducer(streaming, {
      type: 'turn_done', requestId: 'request-correction',
      result: {
        session_id: 'task-1', answer: '已重算',
        revoked_actions: [{ action_ids: ['action-old'], reason: '用户纠正或修改条件' }],
      },
    })

    expect(completed.messages[0].invalidated).toBe(true)
    expect(completed.messages[0].invalidated_reason).toContain('纠正')
  })

  it('history_prepended 把早页插到最前且不打断进行中的流式轮次', () => {
    const streaming = agentConversationReducer(initialAgentConversationState, {
      type: 'turn_started', requestId: 'request-1', message: '继续推进', context: { type: 'job', id: 154 }, retry: false,
    })
    const prepended = agentConversationReducer(streaming, {
      type: 'history_prepended',
      messages: [
        { role: 'user', content: '很早的问题' },
        { role: 'assistant', content: '很早的回答' },
      ],
    })

    expect(prepended.phase).toBe('streaming')
    expect(prepended.activeRequestId).toBe('request-1')
    expect(prepended.messages.map(message => message.content)).toEqual(['很早的问题', '很早的回答', '继续推进', ''])
    // 插入后流式文本仍落到当前轮（最后一条）。
    const withText = agentConversationReducer(prepended, { type: 'turn_text', requestId: 'request-1', content: '生成中' })
    expect(withText.messages[withText.messages.length - 1].content).toBe('生成中')
  })
})

describe('名单卡刷新', () => {
  it('card_refreshed 只更新被点击的那张卡，同岗位其他名单卡不受影响', () => {
    const plainCard = {
      type: 'candidate_list', title: '岗位 137 候选名单',
      context: { type: 'job', id: 137 }, summary: { total: 277 },
    }
    const gradeCard = {
      type: 'candidate_list', title: '岗位 137 严格筛选名单', filter_mode: 'grade_filter',
      context: { type: 'job', id: 137 }, summary: { total: 17 },
    }
    const state = {
      ...initialAgentConversationState,
      messages: [
        { role: 'assistant' as const, content: '全量名单', action_card: plainCard },
        { role: 'assistant' as const, content: '严格筛选名单', action_card: gradeCard },
      ],
    }
    const refreshedCard = { ...gradeCard, summary: { total: 16 } }
    const next = agentConversationReducer(state, {
      type: 'card_refreshed', sourceCard: gradeCard, content: '刷新后的严格筛选名单', action_card: refreshedCard,
    })

    // 被刷新的严格筛选卡更新；同岗位的普通全量卡保持原样
    expect(next.messages[1].content).toBe('刷新后的严格筛选名单')
    expect(next.messages[1].action_card).toBe(refreshedCard)
    expect(next.messages[0].content).toBe('全量名单')
    expect(next.messages[0].action_card).toBe(plainCard)
  })
})
