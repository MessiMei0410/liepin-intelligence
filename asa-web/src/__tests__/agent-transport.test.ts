import { describe, expect, it, vi } from 'vitest'
import { createAgentTurn, brainMode, parseAgentSse, recordDshTurn, streamAgentTurn, DSH_RECORD_FAILED_NOTICE } from '../agent/transport'
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

  it('解析 thinking 事件（DSH 思考过程流），契约漂移同样转错误', () => {
    expect(parseAgentSse('event: thinking\ndata: {"content":"先分析岗位画像"}\n\n')).toEqual([
      { type: 'thinking', data: { content: '先分析岗位画像' } },
    ])
    expect(parseAgentSse('event: thinking\ndata: {"content":42}\n\n')).toEqual([
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
      // 只统计真正的 /turn 调用；DSH 轮次回填（record-turn）等辅助调用不计入鉴权断言
      if (!url.includes('/turn')) {
        return { ok: true, json: async () => ({}) } as unknown as Response
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

  it('默认走 DSH，?brain=copilot 显式回退', () => {
    history.replaceState(null, '', '/')
    expect(brainMode()).toBe('dsh')
    history.replaceState(null, '', '/?brain=copilot')
    expect(brainMode()).toBe('copilot')
    history.replaceState(null, '', '/?brain=dsh')
    expect(brainMode()).toBe('dsh')
    history.replaceState(null, '', '/')
  })

  it('DSH 轮次完成后回填 Core；回填重试仍失败时给出 persist_failed 提示、不影响流式结果', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const sse = 'event: done\r\ndata: {"ok":true,"session_id":"s-dsh","answer":"完成"}\r\n\r\n'
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

    const calls: Array<{ url: string; body?: string }> = []
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? String(init.body) : undefined })
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: false, status: 500, json: async () => ({}) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-dsh', '你好', { type: 'page' }, 'request-9'), new AbortController().signal, event => events.push(event))
      // 回填先于 streamAgentTurn resolve（上层 refreshSessions 不会抢在回填前读到旧列表）
      const record = calls.find(call => call.url.includes('/api/v1/copilot/sessions/record-turn'))
      expect(record).toBeTruthy()
      expect(JSON.parse(record?.body || '{}')).toMatchObject({
        session_id: 's-dsh', request_id: 'request-9', message: '你好', answer: '完成', source: 'dsh',
      })
      // 回填 500：按指数退避共尝试 3 次（2026-08-19 dogfood P0：Core 抖动单次失败丢整段持久化）
      expect(calls.filter(call => call.url.includes('/api/v1/copilot/sessions/record-turn'))).toHaveLength(3)
      // 最终失败：done 照常（不阻断使用），随后补一条 persist_failed 供消息流可见提示
      expect(events.map(event => event.type)).toEqual(['done', 'persist_failed'])
      expect(events[1]).toEqual({ type: 'persist_failed', data: { message: DSH_RECORD_FAILED_NOTICE } })
    } finally {
      vi.unstubAllGlobals()
    }
  }, 15000)

  it('recordDshTurn：瞬时网络异常指数退避重试，第三次成功返回 true', async () => {
    let attempts = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => {
      attempts += 1
      if (attempts < 3) throw new Error('connection reset')
      return { ok: true, json: async () => ({}) } as unknown as Response
    }))
    try {
      const ok = await recordDshTurn(
        createAgentTurn('s-1', '你好', { type: 'page' }, 'request-r1'),
        { session_id: 's-1', answer: '完成' },
        { baseDelayMs: 0 },
      )
      expect(ok).toBe(true)
      expect(attempts).toBe(3)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('recordDshTurn：非 2xx 视为失败并重试，最终失败返回 false', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => ({ ok: false, status: 502, json: async () => ({}) } as unknown as Response))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const ok = await recordDshTurn(
        createAgentTurn('s-1', '你好', { type: 'page' }, 'request-r2'),
        { session_id: 's-1', answer: '完成' },
        { baseDelayMs: 0 },
      )
      expect(ok).toBe(false)
      expect(fetchMock).toHaveBeenCalledTimes(3)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('record-turn 回填完成前 streamAgentTurn 不 resolve（任务栏刷新不会抢到旧列表）', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const sse = 'event: done\r\ndata: {"ok":true,"session_id":"s-race","answer":"完成"}\r\n\r\n'
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

    let releaseRecord!: () => void
    const recordGate = new Promise<void>(resolve => { releaseRecord = resolve })
    let recordRequested = false
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        recordRequested = true
        await recordGate
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      let streamResolved = false
      const pending = streamAgentTurn(createAgentTurn('s-race', '你好', { type: 'page' }, 'request-race'), new AbortController().signal, () => {})
        .then(() => { streamResolved = true })
      // done 已消费、回填请求已发出但未返回：流式 Promise 必须仍在等待
      await vi.waitFor(() => expect(recordRequested).toBe(true))
      expect(streamResolved).toBe(false)
      releaseRecord()
      await pending
      expect(streamResolved).toBe(true)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('解析 DSH 常驻服务器透传的 card 事件', () => {
    const events = parseAgentSse('event: card\ndata: {"type":"candidate_list","context":{"type":"job","id":142},"summary":{"total":7}}\n\n')
    expect(events).toEqual([
      { type: 'card', data: { type: 'candidate_list', context: { type: 'job', id: 142 }, summary: { total: 7 } } },
    ])
  })

  it('DSH card 事件合并进 done（不单独转发），回填 record-turn 带 action_card', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const card = { type: 'candidate_list', context: { type: 'job', id: 142 }, summary: { total: 7 } }
    const sse = [
      'event: progress\r\ndata: {"message":"委托 Copilot 做领域分析…"}\r\n\r\n',
      `event: card\r\ndata: ${JSON.stringify(card)}\r\n\r\n`,
      'event: text\r\ndata: {"content":"名单如下"}\r\n\r\n',
      'event: done\r\ndata: {"ok":true,"session_id":"s-card","answer":"名单如下"}\r\n\r\n',
    ].join('')
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

    const calls: Array<{ url: string; body?: string }> = []
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? String(init.body) : undefined })
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-card', '名单给我', { type: 'job', id: 142 }, 'request-card'), new AbortController().signal, event => events.push(event))
      // card 不单独转发（上层事件循环只认 context/progress/text/done/error），合并进 done
      expect(events.map(event => event.type)).toEqual(['progress', 'text', 'done'])
      expect(events[2]).toEqual({ type: 'done', data: expect.objectContaining({ answer: '名单如下', action_card: card }) })
      await new Promise(resolve => setTimeout(resolve, 0))
      const record = calls.find(call => call.url.includes('/api/v1/copilot/sessions/record-turn'))
      expect(record).toBeTruthy()
      expect(JSON.parse(record?.body || '{}')).toMatchObject({
        session_id: 's-card', request_id: 'request-card', source: 'dsh', action_card: card,
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('DSH done 带 suggested_actions/references：done 事件透传，回填 record-turn 一并带上', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const suggestedActions = [
      { type: 'open_workflow', id: 'workflow_aaa', label: '查看并审批' },
      { type: 'open_candidate', id: 531, label: '打开人选' },
    ]
    const references = [
      { type: 'workflow', id: 'workflow_aaa', label: 'R3 外部寻访审批' },
      { type: 'candidate', id: 531, label: '张三', subtitle: '某半导体' },
    ]
    const sse = [
      'event: text\r\ndata: {"content":"有 2 条待审批"}\r\n\r\n',
      `event: done\r\ndata: ${JSON.stringify({ ok: true, session_id: 's-act', answer: '有 2 条待审批', suggested_actions: suggestedActions, references })}\r\n\r\n`,
    ].join('')
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

    const calls: Array<{ url: string; body?: string }> = []
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? String(init.body) : undefined })
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-act', '查一下待审批', { type: 'page' }, 'request-act'), new AbortController().signal, event => events.push(event))
      expect(events.map(event => event.type)).toEqual(['text', 'done'])
      expect(events[1]).toEqual({
        type: 'done',
        data: expect.objectContaining({ suggested_actions: suggestedActions, references }),
      })
      await new Promise(resolve => setTimeout(resolve, 0))
      const record = calls.find(call => call.url.includes('/api/v1/copilot/sessions/record-turn'))
      expect(record).toBeTruthy()
      expect(JSON.parse(record?.body || '{}')).toMatchObject({
        session_id: 's-act', request_id: 'request-act', source: 'dsh',
        suggested_actions: suggestedActions, references,
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('DSH done 带 Copilot 委托载荷字段：done 事件保留，回填 record-turn 一并带上', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const delegate = {
      understanding_card: { show: true, summary: '我理解为…' },
      execution_receipt: { state: '已生成建议' },
      analysis_card: { headline: '候选人分档', next_step: '核验 2 人' },
      business_focus: { client: '士兰微', action: '寻访' },
      model_participation: { mode: 'model_tools', label: '模型生成 + 工具证据', model: 'deepseek-v4' },
      workflow_id: 'workflow_aaa',
      workflow_progress: {
        workflow_id: 'workflow_aaa', status: 'running', completed: 1, total: 4,
        label: '寻访中', pending_approvals: [],
      },
      action_cards: [{ type: 'candidate_list', summary: { total: 7 } }],
    }
    const sse = [
      'event: text\r\ndata: {"content":"名单如下"}\r\n\r\n',
      `event: done\r\ndata: ${JSON.stringify({ ok: true, session_id: 's-del', answer: '名单如下', ...delegate })}\r\n\r\n`,
    ].join('')
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

    const calls: Array<{ url: string; body?: string }> = []
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? String(init.body) : undefined })
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-del', '名单给我', { type: 'job', id: 142 }, 'request-del'), new AbortController().signal, event => events.push(event))
      expect(events.map(event => event.type)).toEqual(['text', 'done'])
      // done 事件保留委托载荷：turn_done 挂消息后理解卡/执行回执/焦点/进度卡可渲染
      expect(events[1]).toEqual({ type: 'done', data: expect.objectContaining(delegate) })
      await new Promise(resolve => setTimeout(resolve, 0))
      const record = calls.find(call => call.url.includes('/api/v1/copilot/sessions/record-turn'))
      expect(record).toBeTruthy()
      expect(JSON.parse(record?.body || '{}')).toMatchObject({
        session_id: 's-del', request_id: 'request-del', source: 'dsh', ...delegate,
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('DSH 模式 thinking 事件按序透传（不合并进 done）', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const sse = [
      'event: progress\r\ndata: {"message":"DSH 编排中…"}\r\n\r\n',
      'event: thinking\r\ndata: {"content":"先分析岗位"}\r\n\r\n',
      'event: thinking\r\ndata: {"content":"，再查人选"}\r\n\r\n',
      'event: text\r\ndata: {"content":"结论如下"}\r\n\r\n',
      'event: done\r\ndata: {"ok":true,"session_id":"s-think","answer":"结论如下"}\r\n\r\n',
    ].join('')
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

    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-think', '分析一下', { type: 'page' }, 'request-think'), new AbortController().signal, event => events.push(event))
      expect(events.map(event => event.type)).toEqual(['progress', 'thinking', 'thinking', 'text', 'done'])
      expect(events[1]).toEqual({ type: 'thinking', data: { content: '先分析岗位' } })
      expect(events[2]).toEqual({ type: 'thinking', data: { content: '，再查人选' } })
      // thinking 不并入 done（done 只合并 card/confirm_request）
      expect(events[4]).toEqual({ type: 'done', data: expect.not.objectContaining({ thinking: expect.anything() }) })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('解析 DSH 常驻服务器透传的 subagent 增量事件', () => {
    const events = parseAgentSse([
      'event: subagent\ndata: {"event":"start","id":"run-1","label":"背调甲","status":"running"}\n\n',
      'event: subagent\ndata: {"event":"end","id":"run-1","status":"done","summary":"已核实"}\n\n',
    ].join(''))
    expect(events).toEqual([
      { type: 'subagent', data: { event: 'start', id: 'run-1', label: '背调甲', status: 'running' } },
      { type: 'subagent', data: { event: 'end', id: 'run-1', status: 'done', summary: '已核实' } },
    ])
  })

  it('DSH subagent 事件流式透传且聚合进 done.subagents，回填 record-turn 一并带上', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const sse = [
      'event: progress\r\ndata: {"message":"DSH 编排中…"}\r\n\r\n',
      'event: subagent\r\ndata: {"event":"start","id":"run-1","label":"背调甲","status":"running"}\r\n\r\n',
      'event: subagent\r\ndata: {"event":"start","id":"run-2","label":"背调乙","status":"running"}\r\n\r\n',
      'event: subagent\r\ndata: {"event":"end","id":"run-1","status":"done","summary":"甲已核实"}\r\n\r\n',
      'event: text\r\ndata: {"content":"背调结果如下"}\r\n\r\n',
      'event: done\r\ndata: {"ok":true,"session_id":"s-sub","answer":"背调结果如下"}\r\n\r\n',
    ].join('')
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

    const calls: Array<{ url: string; body?: string }> = []
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? String(init.body) : undefined })
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-sub', '背调这两个人', { type: 'page' }, 'request-sub'), new AbortController().signal, event => events.push(event))
      // subagent 增量事件流式透传给上层（AgentWorkspace 显式处理），done 聚合终态快照：
      // run-1 完成带摘要，run-2 仍 running（后台委派轮末未 settle）。
      expect(events.map(event => event.type)).toEqual(['progress', 'subagent', 'subagent', 'subagent', 'text', 'done'])
      expect(events[1]).toEqual({ type: 'subagent', data: { event: 'start', id: 'run-1', label: '背调甲', status: 'running' } })
      expect(events[5]).toEqual({
        type: 'done',
        data: expect.objectContaining({
          subagents: [
            { id: 'run-1', label: '背调甲', status: 'done', summary: '甲已核实' },
            { id: 'run-2', label: '背调乙', status: 'running' },
          ],
        }),
      })
      await new Promise(resolve => setTimeout(resolve, 0))
      const record = calls.find(call => call.url.includes('/api/v1/copilot/sessions/record-turn'))
      expect(record).toBeTruthy()
      expect(JSON.parse(record?.body || '{}')).toMatchObject({
        session_id: 's-sub', request_id: 'request-sub', source: 'dsh',
        subagents: [
          { id: 'run-1', label: '背调甲', status: 'done', summary: '甲已核实' },
          { id: 'run-2', label: '背调乙', status: 'running' },
        ],
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('DSH done 自带 subagents 时不被流式聚合覆盖', async () => {
    vi.stubGlobal('location', { search: '?brain=dsh' })
    const encoder = new TextEncoder()
    const serverSnapshot = [{ id: 'run-1', label: '背调甲', status: 'done', summary: '甲已核实' }]
    const sse = [
      'event: subagent\r\ndata: {"event":"start","id":"run-1","label":"背调甲","status":"running"}\r\n\r\n',
      `event: done\r\ndata: ${JSON.stringify({ ok: true, session_id: 's-sub2', answer: '好', subagents: serverSnapshot })}\r\n\r\n`,
    ].join('')
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

    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/dsh-config')) {
        return { ok: true, json: async () => ({ token: 'tok-1', url: 'http://127.0.0.1:8891/turn' }) } as unknown as Response
      }
      if (url.includes('/api/v1/copilot/sessions/record-turn')) {
        return { ok: true, json: async () => ({ ok: true }) } as unknown as Response
      }
      return streamResponse()
    }))

    try {
      const events: AgentSseEvent[] = []
      await streamAgentTurn(createAgentTurn('s-sub2', '背调', { type: 'page' }, 'request-sub2'), new AbortController().signal, event => events.push(event))
      expect(events[1]).toEqual({ type: 'done', data: expect.objectContaining({ subagents: serverSnapshot }) })
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
