import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentObjectEmbed } from '../agent/AgentObjectEmbed'
import { AgentWorkspace } from '../agent/AgentWorkspace'
import type { Workbench } from '../api'
import type { AgentContext } from '../agent/transport'
import { candidateDetail, mockResponse, plannedWorkflow } from './helpers'

const workbench: Workbench = {
  ok: true,
  version: 'v1',
  summary: { pending: 3, running: 1, delivered: 2, total: 6 },
  items: [],
}

// SSE 流式响应替身：transport.ts 只读取 ok 与 body.getReader()。
const streamResponse = (payload: string) => ({
  ok: true,
  body: {
    getReader: () => {
      let sent = false
      return {
        read: () => Promise.resolve(sent
          ? { value: undefined, done: true }
          : (sent = true, { value: new TextEncoder().encode(payload), done: false })),
      }
    },
  },
}) as unknown as Response

const renderWorkspace = (context: AgentContext) => render(<AgentWorkspace dashboard={{}} workbench={workbench} templates={[]} context={context}
  onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
  onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

describe('Agent workspace', () => {
  beforeEach(() => localStorage.clear())
  it('空任务显示今日摘要并可新建任务', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      if (String(input).includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    }))
    render(<AgentWorkspace dashboard={{ counts: { active_jobs: 4, candidates: 18 } }} workbench={workbench} templates={[]}
      context={{ type: 'page', page: 'agent' }} onOpenAnalysis={() => {}} onRunTemplate={() => {}}
      onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '今日概况' })).toHaveTextContent('3')
    fireEvent.click(screen.getByRole('button', { name: '新任务' }))
    expect(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...')).toHaveValue('')
  })

  it('从任务列表恢复服务端消息和业务焦点', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: { context: { type: 'job', id: 154 }, client: '士兰微', job: { title: '电源专家' } },
        messages: [{ role: 'user', content: '继续找人' }, { role: 'assistant', content: '已恢复任务' }],
      })
      return mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '继续找人', preview: '已恢复任务', message_count: 2, updated_at: '2026-08-03' }] })
    }))
    render(<AgentWorkspace dashboard={{}} workbench={workbench} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
  })

  it('支持服务端搜索、内联重命名和二次确认归档任务', async () => {
    const allSessions = [
      { session_id: 'task-1', title: '士兰微寻访', preview: '继续找人', message_count: 2 },
      { session_id: 'task-2', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
    ]
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/api/v1/copilot/sessions?')) {
        const q = new URLSearchParams(url.split('?')[1] || '').get('q') || ''
        return mockResponse({ ok: true, sessions: q ? allSessions.filter(item => item.title.includes(q)) : allSessions })
      }
      if (url.includes('/api/v1/copilot/sessions/task-1') && init?.method === 'PATCH') return mockResponse({ ok: true, session_id: 'task-1', title: '士兰微电源寻访', archived: false, business_focus: null })
      return mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentWorkspace dashboard={{}} workbench={workbench} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    fireEvent.change(await screen.findByLabelText('搜索任务'), { target: { value: '士兰微' } })
    // 300ms 防抖后走服务端 q 参数
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes(`q=${encodeURIComponent('士兰微')}`))).toBe(true))
    await waitFor(() => expect(screen.queryByText('长越岗位分析')).not.toBeInTheDocument())
    expect(screen.getAllByText('士兰微寻访').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: '重命名任务：士兰微寻访' }))
    const rename = screen.getByRole('form', { name: '重命名任务' })
    fireEvent.change(within(rename).getByLabelText('任务名称'), { target: { value: '士兰微电源寻访' } })
    fireEvent.submit(rename)
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).includes('/task-1') && init?.method === 'PATCH')).toBe(true))

    fireEvent.click(screen.getByRole('button', { name: '归档任务：士兰微电源寻访' }))
    fireEvent.click(screen.getByRole('button', { name: '确认归档任务：士兰微电源寻访' }))
    await waitFor(() => expect(screen.queryByText('士兰微电源寻访')).not.toBeInTheDocument())
  })

  it('显示并可解除当前任务焦点', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/sessions?')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({ ok: true, session_id: 'task-1', title: '任务', archived: false, business_focus: null })))
    render(<AgentWorkspace dashboard={{}} workbench={workbench} templates={[]} context={{ type: 'job', id: 154, client: '士兰微', job: '电源专家' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
    fireEvent.click(screen.getByRole('button', { name: '解除任务焦点' }))
    expect(screen.queryByRole('status', { name: '当前任务焦点' })).not.toBeInTheDocument()
  })

  it('带着新业务上下文恢复不同焦点任务时提示冲突而不是静默覆盖', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: { context: { type: 'job', id: 154 }, client: '士兰微', job: { title: '电源专家' } },
        messages: [{ role: 'user', content: '继续找人' }, { role: 'assistant', content: '甲岗位进展' }],
      })
      return mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'job', id: 155, client: '长越', job: '算法专家' })

    const conflict = await screen.findByRole('alert')
    expect(conflict).toHaveTextContent('你正带着新的业务上下文进入，当前任务焦点为 士兰微 / 电源专家')
    expect(screen.getByText('甲岗位进展')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '继续当前任务' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
  })

  it('冲突提示中选择以新上下文新建任务会保留新上下文', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: { context: { type: 'job', id: 154 }, client: '士兰微', job: { title: '电源专家' } },
        messages: [{ role: 'user', content: '继续找人' }, { role: 'assistant', content: '甲岗位进展' }],
      })
      return mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'job', id: 155, client: '长越', job: '算法专家' })

    fireEvent.click(await screen.findByRole('button', { name: '以新上下文新建任务' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('甲岗位进展')).not.toBeInTheDocument()
    expect(localStorage.getItem('asaAgentSessionId')).toBeNull()
    expect(screen.getByRole('status', { name: '当前任务焦点' })).toHaveTextContent('长越 / 算法专家')
  })

  it('解除焦点失败时保留消息列表并展示非破坏性错误', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (init?.method === 'PATCH') return mockResponse({ detail: 'Core 暂不可用' }, false, 500)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: { context: { type: 'job', id: 154 }, client: '士兰微', job: { title: '电源专家' } },
        messages: [{ role: 'user', content: '继续找人' }, { role: 'assistant', content: '已有进展' }],
      })
      return mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('已有进展')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '解除任务焦点' }))
    expect(await screen.findByText('Core 暂不可用')).toBeInTheDocument()
    expect(screen.getByText('已有进展')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
  })

  it('流式 error 之后到达的 done 不会洗白失败状态', async () => {
    const stream = [
      'event: context\ndata: {"session_id":"task-9"}\n\n',
      'event: error\ndata: {"error":"模型调用失败"}\n\n',
      'event: text\ndata: {"content":"迟到内容"}\n\n',
      'event: done\ndata: {"ok":true,"session_id":"task-9","answer":"迟到答案"}\n\n',
    ].join('')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/stream')
      ? streamResponse(stream)
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...'), { target: { value: '推进一下' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('模型调用失败')).toBeInTheDocument()
    expect(screen.queryByText('迟到答案')).not.toBeInTheDocument()
    expect(screen.queryByText('迟到内容')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('done 返回与 context 不一致的 session 时按失败处理且不写入', async () => {
    const stream = [
      'event: context\ndata: {"session_id":"task-1"}\n\n',
      'event: done\ndata: {"ok":true,"session_id":"task-2","answer":"完成"}\n\n',
    ].join('')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/stream')
      ? streamResponse(stream)
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...'), { target: { value: '推进一下' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('Agent 返回的会话与本轮不一致，已放弃写入')).toBeInTheDocument()
    expect(localStorage.getItem('asaAgentSessionId')).toBe('task-1')
    expect(screen.queryByText('完成')).not.toBeInTheDocument()
  })

  it('done 事件 ok=false 时按失败处理并展示后端错误文案', async () => {
    const stream = [
      'event: context\ndata: {"session_id":"task-1"}\n\n',
      'event: done\ndata: {"ok":false,"session_id":"task-1","answer":"","error":"任务已被归档"}\n\n',
    ].join('')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/stream')
      ? streamResponse(stream)
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...'), { target: { value: '推进一下' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('任务已被归档')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('恢复死会话失败时清理本地会话标记', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-dead')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/copilot/sessions/task-dead?limit=100')
      ? mockResponse({ detail: '任务不存在或已归档' }, false, 404)
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('任务不存在或已归档')).toBeInTheDocument()
    expect(localStorage.getItem('asaAgentSessionId')).toBeNull()
  })

  it('手动停止生成后仍可重试该轮', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => String(input).includes('/api/v1/copilot/stream')
      ? ({
        ok: true,
        body: {
          getReader: () => ({
            read: () => new Promise((_resolve, reject) => {
              init?.signal?.addEventListener('abort', () => reject(new DOMException('已中止', 'AbortError')))
            }),
          }),
        },
      }) as unknown as Response
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...'), { target: { value: '推进一下' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    fireEvent.click(await screen.findByRole('button', { name: '停止生成' }))
    expect(await screen.findByText('已停止生成')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('恢复任务期间禁用输入和发送', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    let resolveRestore: (value: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return new Promise<Response>(resolve => { resolveRestore = resolve })
      return mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('恢复任务')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()

    resolveRestore(mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null }))
    await waitFor(() => expect(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...')).toBeEnabled())
  })

  it('重命名提交进行中时忽略回车触发的重复提交', async () => {
    let resolvePatch: (value: Response) => void = () => {}
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (init?.method === 'PATCH') return new Promise<Response>(resolve => { resolvePatch = resolve })
      if (url.includes('/api/v1/copilot/sessions?')) return mockResponse({ ok: true, sessions: [
        { session_id: 'task-1', title: '士兰微寻访', preview: '继续找人', message_count: 2 },
      ] })
      return mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.click(await screen.findByRole('button', { name: '重命名任务：士兰微寻访' }))
    const rename = screen.getByRole('form', { name: '重命名任务' })
    fireEvent.change(within(rename).getByLabelText('任务名称'), { target: { value: '士兰微电源寻访' } })
    fireEvent.submit(rename)
    fireEvent.submit(rename)
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(1)

    resolvePatch(mockResponse({ ok: true, session_id: 'task-1', title: '士兰微电源寻访', archived: false, business_focus: null }))
    await waitFor(() => expect(screen.queryByRole('form', { name: '重命名任务' })).not.toBeInTheDocument())
  })

  it('服务端搜索请求失败时回落本地过滤', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/copilot/sessions?')) {
        if (url.includes('q=')) return mockResponse({ detail: 'Core 暂不可用' }, false, 500)
        return mockResponse({ ok: true, sessions: [
          { session_id: 'task-1', title: '士兰微寻访', preview: '继续找人', message_count: 2 },
          { session_id: 'task-2', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
        ] })
      }
      return mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('长越岗位分析')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('搜索任务'), { target: { value: '士兰微' } })
    await waitFor(() => expect(screen.queryByText('长越岗位分析')).not.toBeInTheDocument())
    expect(screen.getAllByText('士兰微寻访').length).toBeGreaterThan(0)
  })

  it('清空搜索后恢复默认任务列表', async () => {
    const allSessions = [
      { session_id: 'task-1', title: '士兰微寻访', preview: '继续找人', message_count: 2 },
      { session_id: 'task-2', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
    ]
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/copilot/sessions?')) {
        const q = new URLSearchParams(url.split('?')[1] || '').get('q') || ''
        return mockResponse({ ok: true, sessions: q ? allSessions.filter(item => item.title.includes(q)) : allSessions })
      }
      return mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(await screen.findByLabelText('搜索任务'), { target: { value: '士兰微' } })
    await waitFor(() => expect(screen.queryByText('长越岗位分析')).not.toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('搜索任务'), { target: { value: '' } })
    expect(await screen.findByText('长越岗位分析')).toBeInTheDocument()
    // 恢复默认列表的请求不带 q 参数
    const listCalls = fetchMock.mock.calls.map(([input]) => String(input)).filter(url => url.includes('/api/v1/copilot/sessions?'))
    expect(listCalls[listCalls.length - 1]).not.toContain('q=')
  })

  it('progress 事件显示为临时状态行，正文到达后清除且不写入消息列表', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    const chunks = [
      'event: context\ndata: {"session_id":"task-1"}\n\nevent: progress\ndata: {"message":"梳理岗位需求"}\n\n',
      'event: text\ndata: {"content":"部分答案"}\n\nevent: done\ndata: {"ok":true,"session_id":"task-1","answer":"部分答案"}\n\n',
    ]
    let index = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/stream')
      ? ({
        ok: true,
        body: {
          getReader: () => ({
            read: async () => {
              if (index > 0) await gate
              return index < chunks.length
                ? { value: new TextEncoder().encode(chunks[index++]), done: false }
                : { value: undefined, done: true }
            },
          }),
        },
      }) as unknown as Response
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标...'), { target: { value: '推进一下' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('正在处理：梳理岗位需求')).toBeInTheDocument()
    expect(screen.queryByText('部分答案')).not.toBeInTheDocument()

    release()
    expect(await screen.findByText('部分答案')).toBeInTheDocument()
    expect(screen.queryByText('正在处理：梳理岗位需求')).not.toBeInTheDocument()
  })

  it('服务端焦点有值时优先于本地附着上下文文案，冲突提示不受影响', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: { context: { type: 'job', id: 154 }, client: '士兰微', job: { title: '电源专家' } },
        messages: [{ role: 'user', content: '继续找人' }, { role: 'assistant', content: '甲岗位进展' }],
      })
      return mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'job', id: 155, client: '长越', job: '算法专家' })

    // 冲突提示照常出现，且焦点栏以服务端 business_focus 为准而非本地「长越 / 算法专家」
    expect(await screen.findByRole('alert')).toHaveTextContent('你正带着新的业务上下文进入，当前任务焦点为 士兰微 / 电源专家')
    const focusBar = screen.getByRole('status', { name: '当前任务焦点' })
    expect(focusBar).toHaveTextContent('士兰微 / 电源专家')
    expect(focusBar).not.toHaveTextContent('长越')
  })

  it('服务端无焦点时回落到本地附着上下文文案', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/sessions?')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({ ok: true, session_id: 'task-1', title: '任务', archived: false, business_focus: null })))
    renderWorkspace({ type: 'job', id: 154, client: '士兰微', job: '电源专家' })

    expect(await screen.findByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
  })

  it('恢复拉满 100 条消息时提示仅显示最近 100 条', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    const messages = Array.from({ length: 100 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: `历史消息 ${index + 1}` }))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/copilot/sessions/task-1?limit=100')
      ? mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages })
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('仅显示最近 100 条消息')).toBeInTheDocument()
    expect(screen.getByText('历史消息 100')).toBeInTheDocument()
  })

  it('恢复消息不足 100 条时不显示截断提示', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/copilot/sessions/task-1?limit=100')
      ? mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: [{ role: 'assistant', content: '已恢复任务' }] })
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
    expect(screen.queryByText('仅显示最近 100 条消息')).not.toBeInTheDocument()
  })
})

describe('Agent object embed', () => {
  it('展开候选人并通过预检后提交操作', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/candidates/1')) return mockResponse({ candidate: candidateDetail })
      if (url.endsWith('/api/v1/candidate-actions/preflight')) return mockResponse({ token: 'preflight-1', impact: '候选人将进入复核通过阶段' })
      if (url.endsWith('/api/v1/candidate-actions/commit')) return mockResponse({ ok: true })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'candidate', id: 1, label: '张三' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开张三' }))
    expect(await screen.findByText('示例科技')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复核通过' }))
    expect(await screen.findByRole('alertdialog')).toHaveTextContent('候选人将进入复核通过阶段')
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/candidate-actions/commit'))).toBe(true))
  })

  it('工作流审批继续走既有 approval decision 接口', async () => {
    const workflow = { ...plannedWorkflow, approvals: [{ approval_id: 'approval-1', title: '批准外部寻访', risk_level: 'R3', status: 'pending' }] }
    const fetchMock = vi.fn<typeof fetch>(async input => String(input).includes('/decision') ? mockResponse({ ok: true }) : mockResponse(workflow))
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'workflow', id: 'wf-1', label: '寻访前端工程师' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开寻访前端工程师' }))
    fireEvent.click(await screen.findByRole('button', { name: '批准本次执行' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/v1/approvals/approval-1/decision'))).toBe(true))
  })

  it('对象 id 非法时展示错误而不是发起请求', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({}))
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'candidate', id: 'abc', label: '异常人选' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开异常人选' }))
    expect(await screen.findByText('对象 ID 无效，无法加载详情')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
