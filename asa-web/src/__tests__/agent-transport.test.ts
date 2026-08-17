import { describe, expect, it, vi } from 'vitest'
import { createAgentTurn, parseAgentSse, streamAgentTurn } from '../agent/transport'
import type { AgentSseEvent } from '../agent/transport'

describe('Agent transport', () => {
  it('兼容旧会话引用中的 null 可选字段', () => {
    expect(parseAgentSse('event: context\ndata: {"session_id":"task-1","references":[{"type":"job","id":154,"label":"电源专家","subtitle":null,"href":null}]}\n\n')).toEqual([
      { type: 'context', data: { session_id: 'task-1', references: [{ type: 'job', id: 154, label: '电源专家', subtitle: undefined, href: undefined }] } },
    ])
  })

  it('接受全局 context 的 null id', () => {
    expect(parseAgentSse('event: context\ndata: {"session_id":"task-1","context":{"type":"page","id":null}}\n\n')).toEqual([
      { type: 'context', data: { session_id: 'task-1', context: { type: 'page', id: null } } },
    ])
  })

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

  it('透传模型参与信息供消息标签展示', () => {
    const events = parseAgentSse('event: done\ndata: {"ok":true,"session_id":"task-1","answer":"完成","model_participation":{"mode":"model","label":"模型生成 + 上下文约束","model":"deepseek-v4-flash"}}\n\n')
    expect(events[0]).toEqual({ type: 'done', data: expect.objectContaining({
      model_participation: { mode: 'model', label: '模型生成 + 上下文约束', model: 'deepseek-v4-flash' },
    }) })
  })

  it('从 done 事件保留理解卡和执行回执', () => {
    const events = parseAgentSse('event: done\ndata: {"ok":true,"session_id":"task-1","answer":"请确认范围","understanding_card":{"show":true,"action_label":"过滤候选池"},"execution_receipt":{"state":"已生成建议","verified":false}}\n\n')
    expect(events[0]).toEqual({ type: 'done', data: expect.objectContaining({
      understanding_card: { show: true, action_label: '过滤候选池' },
      execution_receipt: { state: '已生成建议', verified: false },
    }) })
  })

  it('从 workflow/progress/approvals 合成前端可渲染的工作流摘要', () => {
    const events = parseAgentSse('event: done\ndata: {"ok":true,"session_id":"task-1","answer":"已建立目标","workflow_id":"wf-1","workflow":{"status":"planned","current_stage":"确认推进方案"},"progress":{"completed":0,"total":5},"approvals":[{"approval_id":"a1","status":"pending","risk_level":"R3"}]}\n\n')

    expect(events).toEqual([
      { type: 'done', data: expect.objectContaining({
        workflow_progress: {
          workflow_id: 'wf-1',
          status: 'planned',
          completed: 0,
          total: 5,
          label: '确认推进方案',
          pending_approvals: [{ approval_id: 'a1', status: 'pending', risk_level: 'R3' }],
        },
      }) },
    ])
  })

  it('解析 progress 事件，未知事件仍静默丢弃', () => {
    const events = parseAgentSse([
      'event: progress\ndata: {"message":"正在梳理岗位需求"}\n\n',
      'event: heartbeat\ndata: {"ts":1}\n\n',
      'event: context\ndata: {"session_id":"task-1"}\n\n',
      'event: progress\ndata: {"message":"正在调用模型"}\n\n',
      'event: text\ndata: {"content":"部分答案"}\n\n',
    ].join(''))

    expect(events).toEqual([
      { type: 'progress', data: { message: '正在梳理岗位需求' } },
      { type: 'context', data: { session_id: 'task-1' } },
      { type: 'progress', data: { message: '正在调用模型' } },
      { type: 'text', data: { content: '部分答案' } },
    ])
  })

  it('progress 事件契约漂移转成明确错误', () => {
    expect(parseAgentSse('event: progress\ndata: {"message":42}\n\n')).toEqual([
      { type: 'error', data: { error: 'Agent 返回数据与约定格式不一致' } },
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

  it('DSH 模式下 dsh-config 失败不缓存负结果，成功后才缓存', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const sse = 'event: done\r\ndata: {"ok":true,"session_id":"s-1","answer":"完成"}\r\n\r\n'
    const streamResponse = () => ({
      ok: true,
      body: {
        getReader: () => {
          let sent = false
          return {
            read: () => Promise.resolve(sent
              ? { value: undefined, done: true }
              : (sent = true, { value: encoder.encode(sse), done: false })),
          }
        },
      },
    }) as unknown as Response

    let configCalls = 0
    const turnAuths: Array<string | undefined> = []
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/api/v1/dsh-config')) {
        configCalls += 1
        if (configCalls === 1) throw new Error('core not ready')
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      turnAuths.push((init?.headers as Record<string, string> | undefined)?.Authorization)
      return streamResponse()
    })
    vi.stubGlobal('fetch', fetchMock)

    try {
      const newTurn = () => createAgentTurn('s-1', '你好', { type: 'page' }, 'request-1')
      // 第一次：config 拉取失败 → dev 回退（无 token），但不缓存
      await streamAgentTurn(newTurn(), new AbortController().signal, () => {})
      // 第二次：重新拉取成功 → 带 token，此后缓存
      await streamAgentTurn(newTurn(), new AbortController().signal, () => {})
      expect(configCalls).toBe(2)
      // 第三次：命中缓存，不再拉取
      await streamAgentTurn(newTurn(), new AbortController().signal, () => {})
      expect(configCalls).toBe(2)
      expect(turnAuths).toEqual([undefined, 'Bearer tok-1', 'Bearer tok-1'])
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
