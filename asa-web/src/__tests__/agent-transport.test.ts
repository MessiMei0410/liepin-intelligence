import { describe, expect, it, vi } from 'vitest'
import { createAgentTurn, parseAgentSse, streamAgentTurn } from '../agent/transport'
import type { AgentSseEvent } from '../agent/transport'

describe('Agent transport', () => {
  it('解析 context、text、done 三类 SSE 事件', () => {
    const events = parseAgentSse([
      'event: context\ndata: {"session_id":"task-1","context":{"type":"job","id":154}}\n\n',
      'event: text\ndata: {"content":"正在分析"}\n\n',
      'event: done\ndata: {"ok":true,"session_id":"task-1","answer":"正在分析完成","workflow_id":"wf-1"}\n\n',
    ].join(''))

    expect(events).toEqual([
      { type: 'context', data: { session_id: 'task-1', context: { type: 'job', id: 154 } } },
      { type: 'text', data: { content: '正在分析' } },
      { type: 'done', data: expect.objectContaining({ session_id: 'task-1', workflow_id: 'wf-1' }) },
    ])
  })

  it('同一发送任务的重试复用 request id 和幂等键', () => {
    const turn = createAgentTurn('task-1', '继续寻找 10 人', { type: 'job', id: 154 }, 'request-fixed')
    expect(turn.requestId).toBe('request-fixed')
    expect(turn.idempotencyKey).toBe('agent-task-1-request-fixed')
    expect(turn.retry()).toEqual(turn)
  })

  it('将已知事件的契约漂移转成明确错误', () => {
    expect(parseAgentSse('event: done\ndata: {"session_id":"task-1"}\n\n')).toEqual([
      { type: 'error', data: { error: 'Agent 返回数据与约定格式不一致' } },
    ])
  })

  it('兼容 CRLF 分帧的完整事件流', () => {
    const events = parseAgentSse([
      'event: context\r\ndata: {"session_id":"task-1","context":{"type":"job","id":154}}\r\n\r\n',
      'event: text\r\ndata: {"content":"正在分析"}\r\n\r\n',
      'event: done\r\ndata: {"ok":true,"session_id":"task-1","answer":"完成"}\r\n\r\n',
    ].join(''))

    expect(events).toEqual([
      { type: 'context', data: { session_id: 'task-1', context: { type: 'job', id: 154 } } },
      { type: 'text', data: { content: '正在分析' } },
      { type: 'done', data: expect.objectContaining({ session_id: 'task-1', answer: '完成' }) },
    ])
  })

  it('拼接同一事件的多行 data', () => {
    const events = parseAgentSse('event: done\r\ndata: {"ok":true,\r\ndata: "session_id":"task-1",\r\ndata: "answer":"完成"}\r\n\r\n')
    expect(events).toEqual([
      { type: 'done', data: expect.objectContaining({ session_id: 'task-1', answer: '完成' }) },
    ])
  })

  it('数据块刚好切在事件边界也能按 CRLF 完整分帧', async () => {
    const encoder = new TextEncoder()
    const chunks = [
      'event: context\r\ndata: {"session_id":"task-1"}\r\n',
      '\r\nevent: text\r\ndata: {"content":"分段"}\r\n\r\nevent: done\r\n',
      'data: {"ok":true,"session_id":"task-1","answer":"分段完成"}\r\n\r\n',
    ].map(part => encoder.encode(part))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => ({
      ok: true,
      body: {
        getReader: () => {
          let index = 0
          return {
            read: () => Promise.resolve(index < chunks.length
              ? { value: chunks[index++], done: false }
              : { value: undefined, done: true }),
          }
        },
      },
    }) as unknown as Response))

    const events: AgentSseEvent[] = []
    await streamAgentTurn(createAgentTurn('', '你好', { type: 'page' }, 'request-1'), new AbortController().signal, event => events.push(event))
    expect(events.map(event => event.type)).toEqual(['context', 'text', 'done'])
    expect(events[2]).toEqual({ type: 'done', data: expect.objectContaining({ answer: '分段完成' }) })
  })
})
