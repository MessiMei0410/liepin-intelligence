import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentObjectEmbed } from '../agent/AgentObjectEmbed'
import { AgentWorkspace, buildSessionMarkdown } from '../agent/AgentWorkspace'
import type { Workbench } from '../api'
import type { AgentContext, AgentReference } from '../agent/transport'
import { candidateDetail, mockResponse, plannedWorkflow } from './helpers'
import { FULL_OBJECT_CLOSED_EVENT } from '../agent/navigation'

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

const renderWorkspace = (context: AgentContext, options: { onOpenFullObject?: (reference: AgentReference) => void } = {}) => render(<AgentWorkspace workbench={workbench} templates={[]} context={context}
  onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
  onWorkbenchAction={() => {}} onOpenFullObject={options.onOpenFullObject || (() => {})} />)

describe('Agent workspace', () => {
  beforeEach(() => {
    localStorage.clear()
    // 任务栏折叠偏好：测试默认展开，任务栏交互断言不受折叠影响。
    localStorage.setItem('asaTaskRailCollapsed', '0')
  })
  it('空任务显示今日摘要并可新建任务', async () => {
    const scrollSpy = vi.fn()
    const originalScrollIntoView = window.HTMLElement.prototype.scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = scrollSpy
    try {
      vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
        if (String(input).includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
        return mockResponse({})
      }))
      render(<AgentWorkspace workbench={workbench} templates={[]}
        context={{ type: 'page', page: 'agent' }} onOpenAnalysis={() => {}} onRunTemplate={() => {}}
        onManageTemplate={() => {}} onCreateTemplate={() => {}}
        onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

      expect(await screen.findByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
      // 空 lane（待客户/风险）不占位渲染；有数据的 lane 显示真实计数。
      expect(screen.getByRole('region', { name: '待判断' })).toHaveTextContent('共 3 项')
      expect(screen.queryByRole('region', { name: '待客户' })).not.toBeInTheDocument()
      expect(scrollSpy).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: '新任务' }))
      expect(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）')).toHaveValue('')
    } finally {
      window.HTMLElement.prototype.scrollIntoView = originalScrollIntoView
    }
  })

  it('选择附件后读取正文并随消息发送给 Agent', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/attachments')) return mockResponse({
        ok: true,
        attachment: {
          attachment_id: 'att-xlsx-1', access_token: 'attachment-access-token-for-test', file_name: '探针台CPO方向新增需求.xlsx', file_type: 'xlsx',
          mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size_bytes: 12,
          content_available: true,
          truncated: false, is_image: false, status: '已读取附件正文。',
        },
      })
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        'event: context\ndata: {"session_id":"task-file"}\n\nevent: text\ndata: {"content":"已读取岗位需求"}\n\nevent: done\ndata: {"ok":true,"session_id":"task-file","answer":"已读取岗位需求"}\n\n',
      )
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    const file = new File(['xlsx-content'], '探针台CPO方向新增需求.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    fireEvent.change(screen.getByLabelText('选择附件'), { target: { files: [file] } })

    expect(await screen.findByText('12 B · 已读取附件正文。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toBe(true))
    const streamCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/copilot/stream'))
    const body = JSON.parse(String(streamCall?.[1]?.body || '{}'))
    expect(body.message).toContain('请读取并分析附件')
    expect(body.context.uploaded_attachments[0]).toMatchObject({
      file_name: '探针台CPO方向新增需求.xlsx',
      access_token: 'attachment-access-token-for-test',
    })
    expect(body.context.uploaded_attachments[0]).not.toHaveProperty('extracted_text')
    expect(await screen.findByText('已读取岗位需求')).toBeInTheDocument()
    expect(screen.getByLabelText('消息附件')).toHaveTextContent('探针台CPO方向新增需求.xlsx')
  })

  it('实时展示理解卡并通过显式对象上下文继续歧义指令', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        'event: context\ndata: {"session_id":"task-clarify"}\n\nevent: done\ndata: {"ok":true,"session_id":"task-clarify","answer":"请选择岗位","understanding_card":{"show":true,"message":"过滤这两个岗位候选池","action":"candidate_review","action_label":"评估/复核","confidence":0.82,"target":{"type":"global","label":"当前上下文"},"candidate_options":[{"type":"job","id":9,"client":"长越科技","label":"机械高级工程师","status":"open","updated_at":"2026-08-12"}]}}\n\n',
      )
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })
    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '过滤这两个岗位候选池' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByRole('region', { name: 'ASA 理解卡' })).toHaveTextContent('评估/复核')
    fireEvent.click(screen.getByRole('button', { name: /长越科技机械高级工程师/ }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toHaveLength(2))
    const secondBody = JSON.parse(String(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/copilot/stream'))[1][1]?.body || '{}'))
    expect(secondBody.message).toBe('过滤这两个岗位候选池')
    expect(secondBody.context).toMatchObject({ type: 'job', id: 9, clarification_binding: true })
    expect(screen.getAllByText('过滤这两个岗位候选池')).toHaveLength(1)
  })

  it('候选人待确认卡携带一次性预检信息直达确认端点', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        'event: context\ndata: {"session_id":"task-candidate"}\n\nevent: done\ndata: {"ok":true,"session_id":"task-candidate","answer":"确认推进","pending_intent":{"kind":"candidate_action","action":"advance","action_label":"复核通过","intent_hash":"hash-1","preflight_token":"token-1","message":"这个人选复核通过","candidate":{"id":558,"name":"王先生","client":"士兰微","job":"电源专家","stage":"S1 待复核"}}}\n\n',
      )
      if (url.endsWith('/api/v1/copilot/intents/confirm')) return mockResponse({ ok: true, answer: '已确认并同步到 ASA：王先生复核通过。' })
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'candidate', id: 558 })
    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '这个人选复核通过' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认执行' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/copilot/intents/confirm'))).toBe(true))
    const confirmCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/copilot/intents/confirm'))
    expect(JSON.parse(String(confirmCall?.[1]?.body || '{}'))).toMatchObject({
      intent_hash: 'hash-1', candidate_id: 558, preflight_token: 'token-1', session_id: 'task-candidate',
      intent: { kind: 'candidate_action', action: 'advance' },
    })
    expect(await screen.findByRole('region', { name: '候选人执行回执' })).toHaveTextContent('已完成服务端回查')
  })

  it('done 事件到达后挂载策略建议卡并默认选中全部建议', async () => {
    const strategyPatch = {
      workflow_id: 'workflow-strategy-1',
      workflow_title: '长越科技｜机械高级工程师｜第 2 轮',
      strategy_hash: 'strategy-hash-1',
      changes: [
        { type: 'add_keyword', value: '精密运动平台', clause: '新增关键词「精密运动平台」' },
        { type: 'add_company', value: 'ASMPT', clause: '新增对标公司「ASMPT」' },
        { type: 'add_filter', value: '排除纯销售背景', clause: '新增过滤条件「排除纯销售背景」' },
      ],
    }
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        `event: context\ndata: {"session_id":"task-strategy"}\n\nevent: text\ndata: {"content":"建议补充以下策略项"}\n\nevent: done\ndata: ${JSON.stringify({ ok: true, session_id: 'task-strategy', answer: '建议补充以下策略项', strategy_patch: strategyPatch })}\n\n`,
      )
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'workflow', id: 'workflow-strategy-1' })

    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '把这些建议沉淀进策略' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    const card = await screen.findByRole('region', { name: '寻访策略建议' })
    expect(within(card).getAllByRole('checkbox')).toHaveLength(3)
    expect(within(card).getAllByRole('checkbox').every(input => (input as HTMLInputElement).checked)).toBe(true)
    expect(within(card).getByRole('button', { name: '检查写入内容' })).toBeEnabled()
  })

  it('附件读取失败时阻止发送，移除失败项后恢复文本发送', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/copilot/attachments')
      ? mockResponse({ detail: 'Office 文件结构损坏' }, false, 422)
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByLabelText('选择附件'), {
      target: { files: [new File(['broken'], '损坏需求.xlsx')] },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('Office 文件结构损坏')
    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '分析这个需求' } })
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '移除附件：损坏需求.xlsx' }))
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
  })

  it('Agent 首页可展开人才雷达并保持四入口主导航之外的模块可达', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      if (url.endsWith('/api/v1/radar/scans/latest')) return mockResponse({
        radar_scan: {
          scan_date: '2026-08-04',
          stats: {},
          signals: [{ company: '示例科技', type: 'hiring', summary: '研发岗位增加', as_of: '2026-08-04', source_urls: ['https://example.com'], confidence: 'medium', linked_action: 'mapping' }],
          ranking: [{ company: '示例科技', score: 82, reason: '招聘异动', signal_count: 1 }],
        },
      })
      return mockResponse({})
    }))
    render(<AgentWorkspace jobs={[{ id: 9, client: '示例客户', title: '电源专家', candidate_count: 0, active_candidate_count: 0 }]}
      workbench={workbench} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: '人才雷达' }))

    await waitFor(() => expect(screen.getByRole('region', { name: '人才雷达' })).toHaveTextContent('示例科技'))
    expect(screen.getByRole('button', { name: '收起人才雷达' })).toBeInTheDocument()
  })

  it('Agent 首页按批次展开待判断事项，而不一次渲染全部窗口', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true, sessions: [] })))
    const manyPending: Workbench = {
      ...workbench,
      summary: { pending: 45, running: 0, delivered: 0, total: 45, decision: 45, waiting_client: 0, risk: 0 },
      items: Array.from({ length: 45 }, (_, index) => ({
        item_key: `pending-${index}`, source_revision: 'v1', kind: 'candidate_action', lane: 'decision', priority_score: 100 - index,
        title: `待办 ${index + 1}`, subtitle: '需要处理', status_label: '待复核', reason: '', source_label: 'ASA', inbox_state: 'unread',
        primary_action: { type: 'open_candidate', id: String(index + 1), label: '打开人选' },
      })),
    }
    render(<AgentWorkspace workbench={manyPending} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByText('待办 4')).toBeInTheDocument()
    expect(screen.queryByText('待办 24')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '再显示 20 项' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '再显示 20 项' }))
    expect(screen.getByText('待办 24')).toBeInTheDocument()
    expect(screen.queryByText('待办 25')).not.toBeInTheDocument()
    expect(screen.getByText('共 45 项')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '再显示 20 项' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '收起' })).toBeInTheDocument()
  })

  it('Agent 首页首次拉取未完成时显示加载态，而不是伪“没有待办”', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true, sessions: [] })))
    const loadingWorkbench: Workbench = {
      ok: true, version: 'loading', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [],
    }
    render(<AgentWorkspace workbench={loadingWorkbench} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
    // 加载态：全部 5 个 lane 骨架展示，不出现伪空态文案。
    expect(screen.getAllByText('正在加载…').length).toBe(5)
    expect(screen.queryByText('当前没有待判断事项')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /查看全部/ })).not.toBeInTheDocument()
  })

  it('Agent 首页在服务端截断时标明已加载数量，避免“查看全部”误导为全量', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true, sessions: [] })))
    const truncatedWorkbench: Workbench = {
      ...workbench,
      summary: { pending: 6, running: 0, delivered: 0, total: 6, decision: 6, waiting_client: 0, risk: 0 },
      returned_count: 5, truncated: true,
      items: Array.from({ length: 5 }, (_, index) => ({
        item_key: `pending-${index}`, source_revision: 'v1', kind: 'candidate_action', lane: 'decision', priority_score: 100 - index,
        title: `待办 ${index + 1}`, subtitle: '需要处理', status_label: '待复核', reason: '', source_label: 'ASA', inbox_state: 'unread',
        primary_action: { type: 'open_candidate', id: String(index + 1), label: '打开人选' },
      })),
    }
    render(<AgentWorkspace workbench={truncatedWorkbench} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    // 服务端只返回 5/6 项待办：计数以 summary 真实总数为准，分页承接剩余窗口。
    expect(await screen.findByText('共 6 项')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '再显示 1 项' }))
    expect(screen.getByText('待办 5')).toBeInTheDocument()
    expect(screen.queryByText('待办 6')).not.toBeInTheDocument()
    expect(screen.getByText('共 6 项')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '收起' })).toBeInTheDocument()
  })

  it('Agent 首页按分组展示工作台，空分组自动隐藏', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true, sessions: [] })))
    const onAction = vi.fn()
    const fiveLane: Workbench = {
      ...workbench,
      summary: { pending: 1, running: 1, delivered: 1, total: 4, decision: 1, waiting_client: 1, risk: 0 },
      items: [
        { item_key: 'approval:a1', source_revision: 'r1', kind: 'approval', lane: 'decision', priority_score: 20000, title: '批准多渠道寻访', subtitle: '电源专家寻访', status_label: 'R3 待审批', reason: '外部动作需由顾问确认', source_label: '审批', inbox_state: 'unread', primary_action: { type: 'open_workflow', id: 'wf-1', label: '查看并审批' } },
        { item_key: 'candidate:9', source_revision: 'r2', kind: 'candidate_action', lane: 'waiting_client', priority_score: 100, title: '王先生', subtitle: '士兰微 / 电源专家', status_label: '进行中', reason: '已推荐，待客户反馈', source_label: '人选推进', inbox_state: 'unread', primary_action: { type: 'open_candidate', id: '9', label: '查看' } },
        { item_key: 'workflow:w1', source_revision: 'r3', kind: 'workflow', lane: 'running', priority_score: 6000, title: '第 3 轮寻访', subtitle: '执行多渠道寻访', status_label: '运行中', reason: '', source_label: 'Agent 任务', inbox_state: 'unread', primary_action: { type: 'open_workflow', id: 'w1', label: '查看进度' } },
        { item_key: 'analysis:a1', source_revision: 'r4', kind: 'analysis', lane: 'delivered', priority_score: 0, title: '经营概览已生成', subtitle: '经营概览', status_label: '已交付', reason: '', source_label: '分析', inbox_state: 'unread', primary_action: { type: 'open_analysis', id: 'a1', label: '查看分析' } },
      ],
    }
    render(<AgentWorkspace workbench={fiveLane} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={onAction} onOpenFullObject={() => {}} />)

    for (const label of ['待判断', '运行中', '待客户', '最近交付']) {
      expect(await screen.findByRole('region', { name: label })).toBeInTheDocument()
    }
    expect(screen.getByRole('region', { name: '待判断' })).toHaveTextContent('R3 待审批')
    // 风险/逾期无数据：空分组不占位渲染。
    expect(screen.queryByRole('region', { name: '风险/逾期' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('王先生'))
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ item_key: 'candidate:9' }))
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
    render(<AgentWorkspace workbench={workbench} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
    expect(screen.getByText('会话 ID：task-1')).toBeInTheDocument()
  })

  it('右键任务卡弹出菜单并复制对应会话 ID', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: null,
        messages: [{ role: 'assistant', content: '已恢复任务' }],
      })
      return mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '继续找人', preview: '已恢复任务', message_count: 2, updated_at: '2026-08-03' }] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
    const rail = screen.getByRole('complementary', { name: '任务历史' })
    const card = within(rail).getByText('继续找人').closest('article')
    expect(card).not.toBeNull()
    fireEvent.contextMenu(card as HTMLElement)
    const menu = screen.getByRole('menu', { name: '任务操作' })
    expect(menu).toBeInTheDocument()
    fireEvent.click(within(menu).getByRole('menuitem', { name: /复制会话 ID/ }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('task-1'))
    expect(screen.queryByRole('menu', { name: '任务操作' })).not.toBeInTheDocument()
  })

  it('buildSessionMarkdown 按天分节并标注角色与元信息', () => {
    const markdown = buildSessionMarkdown({
      title: '找人',
      sessionId: 'task-9',
      focus: '士兰微 / 电源专家',
      exportedAt: '2026/08/16 10:00',
      messages: [
        { role: 'user', content: '先看看地图', created_at: '2026-08-15 09:00' },
        { role: 'assistant', content: '已给出地图', created_at: '2026-08-15 09:01' },
        { role: 'user', content: '第二天再问', created_at: '2026-08-16 08:00' },
      ],
    })
    expect(markdown).toContain('# ASA 任务：找人')
    expect(markdown).toContain('消息数：3')
    expect(markdown).toContain('任务焦点：士兰微 / 电源专家')
    expect(markdown).toContain('**你** · 2026-08-15 09:00')
    expect(markdown).toContain('**ASA** · 2026-08-15 09:01')
    expect(markdown.match(/## 2026-08-15/g)).toHaveLength(1)
    expect(markdown).toContain('## 2026-08-16')
  })

  it('⌘⇧N 新建任务：清空当前会话回到空任务首页', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: null,
        messages: [{ role: 'assistant', content: '已恢复任务' }], total: 1, has_more: false,
      })
      return mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '继续找人', preview: '已恢复任务', message_count: 1, updated_at: '2026-08-03' }] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'n', metaKey: true, shiftKey: true })

    expect(await screen.findByText('今天从哪里开始？')).toBeInTheDocument()
    expect(screen.queryByText('已恢复任务')).not.toBeInTheDocument()
  })

  it('? 打开快捷键帮助面板，Esc 关闭；输入框内不触发', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/sessions?')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({})))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.keyDown(document.body, { key: '?' })
    const dialog = await screen.findByRole('dialog', { name: '快捷键' })
    expect(within(dialog).getByText('聚焦输入框')).toBeInTheDocument()
    expect(within(dialog).getByText('新建任务')).toBeInTheDocument()

    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '快捷键' })).not.toBeInTheDocument())

    // 焦点在输入框时按 ? 是打字，不弹帮助。
    const composer = screen.getByLabelText('Agent 消息')
    fireEvent.keyDown(composer, { key: '?' })
    expect(screen.queryByRole('dialog', { name: '快捷键' })).not.toBeInTheDocument()
  })

  it('右键任务卡可导出会话：分页拉全量并下载 Markdown', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    const capturedBlobs: Blob[] = []
    const createObjectURL = vi.fn((blob: Blob) => { capturedBlobs.push(blob); return 'blob:mock' })
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    try {
      const pageOne = Array.from({ length: 200 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: `第${index + 1}条` }))
      const pageTwo = [
        { role: 'user' as const, content: '最早的问题', created_at: '2026-08-01 09:00' },
        { role: 'assistant' as const, content: '最早的回答', created_at: '2026-08-01 09:01' },
      ]
      const fetchMock = vi.fn<typeof fetch>(async input => {
        const url = String(input)
        if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) {
          return mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: [{ role: 'assistant', content: '已恢复任务' }], total: 202, has_more: true })
        }
        if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=200')) {
          return mockResponse({ ok: true, session_id: 'task-1', business_focus: { client: '士兰微', job: { title: '电源专家' } }, messages: pageOne, total: 202, has_more: true })
        }
        if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=200&offset=200')) {
          return mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: pageTwo, total: 202, has_more: false })
        }
        return mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '继续找人', preview: '已恢复任务', message_count: 202, updated_at: '2026-08-03' }] })
      })
      vi.stubGlobal('fetch', fetchMock)
      renderWorkspace({ type: 'page', page: 'agent' })

      expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
      const rail = screen.getByRole('complementary', { name: '任务历史' })
      const card = within(rail).getByText('继续找人').closest('article')
      fireEvent.contextMenu(card as HTMLElement)
      fireEvent.click(within(screen.getByRole('menu', { name: '任务操作' })).getByRole('menuitem', { name: /导出会话/ }))

      await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1))
      const markdown = await capturedBlobs[0].text()
      expect(markdown).toContain('# ASA 任务：继续找人')
      expect(markdown).toContain('消息数：202')
      expect(markdown).toContain('任务焦点')
      // 跨天分节：早于当天的消息带日期小节标题。
      expect(markdown).toContain('## 2026-08-01')
      expect(markdown).toContain('最早的问题')
      expect(markdown).toContain('第200条')
      expect(clickSpy).toHaveBeenCalledTimes(1)
      const anchorDownload = clickSpy.mock.instances[0] as HTMLAnchorElement
      expect(anchorDownload.download).toMatch(/^ASA任务-继续找人-\d{8}-\d{4}\.md$/)
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
      // 对话头也有导出入口（当前会话激活时）。
      expect(screen.getByRole('button', { name: '导出会话', hidden: false })).toBeInTheDocument()
    } finally {
      clickSpy.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('右键任务卡菜单可点击空白或按 Escape 关闭', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({
        ok: true, session_id: 'task-1', business_focus: null, messages: [],
      })
      return mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '继续找人', preview: '已恢复任务', message_count: 2, updated_at: '2026-08-03' }] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    const rail = await screen.findByRole('complementary', { name: '任务历史' })
    await waitFor(() => expect(within(rail).getByText('继续找人')).toBeInTheDocument())
    const card = within(rail).getByText('继续找人').closest('article')
    fireEvent.contextMenu(card as HTMLElement)
    expect(screen.getByRole('menu', { name: '任务操作' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('presentation'))
    expect(screen.queryByRole('menu', { name: '任务操作' })).not.toBeInTheDocument()

    fireEvent.contextMenu(card as HTMLElement)
    expect(screen.getByRole('menu', { name: '任务操作' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('menu', { name: '任务操作' })).not.toBeInTheDocument()
  })

  it('展示模型参与标签并可打开脱敏模型审计', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-model')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/agent/model-audit')) return mockResponse({
        ok: true,
        summary: { total: 3, failed: 1, fallback: 1, avg_duration_ms: 842 },
        items: [{
          call_id: 'llm-1', operation: 'assess_risks', provider: 'api.deepseek.com', model: 'deepseek-v4-flash',
          status: 'failed', validation_status: 'failed', fallback_used: 1, duration_ms: 920,
          input_tokens: 120, output_tokens: 18, request_hash: 'abcdef1234567890',
          request_preview: 'JSON 对象；字段：candidate, job', response_preview: '文本；8 字符',
          error: '模型没有返回合法 JSON', created_at: '2026-08-04 10:00:00', finished_at: '2026-08-04 10:00:01',
        }],
      })
      if (url.endsWith('/api/v1/copilot/sessions/task-model?limit=100')) return mockResponse({
        ok: true, session_id: 'task-model', business_focus: null,
        messages: [{ role: 'assistant', content: '这是带来源标记的回答', model_participation: { mode: 'model', label: '模型生成 + 上下文约束', model: 'deepseek-v4-flash' } }],
      })
      return mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('模型生成 + 上下文约束')).toHaveAttribute('title', 'deepseek-v4-flash')
    fireEvent.click(screen.getByRole('button', { name: '模型输出审计' }))
    const panel = await screen.findByRole('complementary', { name: '模型输出审计' })
    expect(panel).toHaveTextContent('3')
    expect(panel).toHaveTextContent('模型失败，已规则降级')
    expect(panel).toHaveTextContent('结构校验失败')
    expect(panel).toHaveTextContent('JSON 对象；字段：candidate, job')
  })

  it('将助手 Markdown 渲染为可读的标题、列表、加粗和表格', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-markdown')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/copilot/sessions/task-markdown?limit=100')
      ? mockResponse({
        ok: true, session_id: 'task-markdown', business_focus: null,
        messages: [{
          role: 'assistant',
          content: '## 核心结论\n\n**优先处理**\n\n- 候选人甲\n- 候选人乙\n\n| 岗位 | 人数 |\n| --- | ---: |\n| 电源专家 | 3 |',
          references: [{ type: 'job', id: 154, label: '电源专家', subtitle: null, href: null }],
        }],
      })
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByRole('heading', { name: '核心结论' })).toBeInTheDocument()
    expect(screen.getByText('优先处理').tagName).toBe('STRONG')
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByRole('table')).toHaveTextContent('电源专家')
    expect(screen.getByRole('button', { name: '展开电源专家' })).toBeInTheDocument()
    expect(screen.queryByText('**优先处理**')).not.toBeInTheDocument()
  })

  it('工作流回复去重对象卡，并直接展示推进方案摘要', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-workflow')
    const openFull = vi.fn()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      if (String(input).endsWith('/api/v1/workflows/wf-1')) return mockResponse(plannedWorkflow)
      return String(input).endsWith('/api/v1/copilot/sessions/task-workflow?limit=100')
        ? mockResponse({
        ok: true, session_id: 'task-workflow', business_focus: null,
        messages: [{
          role: 'assistant',
          content: '已建立目标：长越科技｜自动化软件高级工程师｜第4轮寻访',
          references: [
            { type: 'job', id: 201, label: '自动化软件高级工程师', subtitle: '长越科技' },
            { type: 'job', id: 202, label: '自动化软件高级工程师', subtitle: '长越科技' },
            { type: 'job', id: 203, label: '打开岗位' },
            { type: 'candidate', id: 301, label: '王** · 待首次评估', subtitle: '其他历史人选' },
            { type: 'candidate', id: 302, label: '胡** · 待首次评估', subtitle: '其他历史人选' },
            { type: 'candidate', id: 303, label: '王先生 · 待首次评估', subtitle: '其他历史人选' },
            { type: 'workflow', id: 'wf-1', label: '查看计划' },
          ],
          suggested_actions: [{ type: 'open_workflow', id: 'wf-1', label: '查看计划' }],
          workflow_id: 'wf-1',
          workflow_progress: { workflow_id: 'wf-1', status: 'planned', completed: 0, total: 5, label: '确认推进方案', pending_approvals: [{ approval_id: 'a1', status: 'pending' }] },
          action_card: {
            context: { type: 'workflow', id: 'wf-1', title: '长越科技｜自动化软件高级工程师｜第4轮寻访' },
            business_summary: {
              task: '从现有人选中整理自动化软件高级工程师的优先名单',
              completed: ['读取岗位范围'],
              current: '整理候选人核验队列',
              deliverable: '优先评估名单，以及每位候选人的命中依据',
              scope_note: '本次不触发外部寻访',
            },
            evidence: [{ label: '当前步骤', value: '内部诊断现有人选与渠道缺口' }, { label: '理解目标', value: '推进长越软件岗位' }],
            blocked_reasons: ['外部动作仍需 R3 单次审批'],
            next_actions: [
              { type: 'open_workflow', id: 'wf-1', label: '查看计划' },
              { type: 'start_workflow', id: 'wf-1', label: '确认计划并准备', plan_ref: { version: 1, plan_hash: 'hash-1' } },
              { type: 'workflow_approval', id: 'approval-1' },
            ],
          },
        }],
        })
        : mockResponse({ ok: true, sessions: [] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' }, { onOpenFullObject: openFull })

    expect(await screen.findByText('本次要做什么')).toBeInTheDocument()
    expect(screen.getByText('从现有人选中整理自动化软件高级工程师的优先名单')).toBeInTheDocument()
    expect(screen.queryByText('优先评估名单，以及每位候选人的命中依据')).not.toBeInTheDocument()
    expect(screen.getByText('整理候选人核验队列')).toBeInTheDocument()
    expect(screen.getByText('0 / 5 步 · 1 个审批待确认')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开长越科技｜自动化软件高级工程师｜第4轮寻访' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '展开自动化软件高级工程师' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '展开打开岗位' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '展开查看计划' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /展开王/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '展开胡** · 待首次评估' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看审批' }))
    expect(openFull).toHaveBeenCalledWith(expect.objectContaining({ type: 'workflow', id: 'wf-1' }))
    fireEvent.click(screen.getByRole('button', { name: '展开长越科技｜自动化软件高级工程师｜第4轮寻访' }))
    expect(screen.queryByRole('region', { name: '执行方案摘要' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看审批' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起长越科技｜自动化软件高级工程师｜第4轮寻访' }))
    fireEvent.click(screen.getByRole('button', { name: '确认计划并准备' }))
    expect(fetch).not.toHaveBeenCalledWith('/api/v1/workflows/wf-1/start', expect.anything())
    fireEvent.click(await screen.findByRole('button', { name: '确认开始' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      '/api/v1/workflows/wf-1/start',
      expect.objectContaining({ method: 'POST', body: expect.stringContaining('hash-1') }),
    ))
  })

  it('桌面任务栏可以隐藏再显示', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/sessions')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.click(await screen.findByRole('button', { name: '隐藏任务栏' }))
    expect(screen.getByRole('button', { name: '显示任务栏' })).toBeInTheDocument()
    expect(screen.queryByLabelText('搜索任务')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '显示任务栏' }))
    expect(screen.getByLabelText('搜索任务')).toBeInTheDocument()
  })

  it('任务栏默认折叠（无偏好），展开后偏好被记住', async () => {
    localStorage.removeItem('asaTaskRailCollapsed')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/sessions')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })))
    renderWorkspace({ type: 'page', page: 'agent' })

    // 无偏好 → 默认折叠：窄条展开按钮可见，任务列表/搜索不可见。
    expect(await screen.findByRole('button', { name: '显示任务栏' })).toBeInTheDocument()
    expect(screen.queryByLabelText('搜索任务')).not.toBeInTheDocument()
    // 展开后写入偏好，下次进入保持展开。
    fireEvent.click(screen.getByRole('button', { name: '显示任务栏' }))
    expect(screen.getByLabelText('搜索任务')).toBeInTheDocument()
    expect(localStorage.getItem('asaTaskRailCollapsed')).toBe('0')
  })

  it('空态快捷指令一键发起对应指令', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        'event: context\ndata: {"session_id":"task-quick"}\n\nevent: text\ndata: {"content":"好的，开始过滤"}\n\nevent: done\ndata: {"ok":true,"session_id":"task-quick","answer":"好的，开始过滤"}\n\n',
      )
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.click(await screen.findByRole('button', { name: /过滤名单/ }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toBe(true))
    const streamCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/copilot/stream'))
    const body = JSON.parse(String(streamCall?.[1]?.body || '{}'))
    expect(body.message).toBe('过滤候选人名单')
    expect(await screen.findByText('好的，开始过滤')).toBeInTheDocument()
  })

  it('折叠任务栏显示任务数量角标', async () => {
    localStorage.removeItem('asaTaskRailCollapsed')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/sessions')
      ? mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '继续找人', preview: '已恢复任务', message_count: 2, updated_at: '2026-08-03' }] })
      : mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByLabelText('1 个任务')).toBeInTheDocument()
  })

  it('工作台板块可折叠收起，计数保留', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true, sessions: [] })))
    const singlePending: Workbench = {
      ...workbench,
      summary: { pending: 1, running: 0, delivered: 0, total: 1, decision: 1, waiting_client: 0, risk: 0 },
      items: [{
        item_key: 'approval:a1', source_revision: 'r1', kind: 'approval', lane: 'decision', priority_score: 1,
        title: '批准多渠道寻访', subtitle: '电源专家寻访', status_label: 'R3 待审批', reason: '', source_label: '审批', inbox_state: 'unread',
        primary_action: { type: 'open_workflow', id: 'wf-1', label: '查看并审批' },
      }],
    }
    render(<AgentWorkspace workbench={singlePending} templates={[]} context={{ type: 'page', page: 'agent' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByRole('button', { name: /批准多渠道寻访/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起待判断' }))
    expect(screen.queryByRole('button', { name: /批准多渠道寻访/ })).not.toBeInTheDocument()
    // 折叠后计数仍在。
    expect(screen.getByRole('region', { name: '待判断' })).toHaveTextContent('共 1 项')
    fireEvent.click(screen.getByRole('button', { name: '展开待判断' }))
    expect(screen.getByRole('button', { name: /批准多渠道寻访/ })).toBeInTheDocument()
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
    render(<AgentWorkspace workbench={workbench} templates={[]} context={{ type: 'page', page: 'agent' }}
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

  it('归档当前任务时清理本地 session 并回到新任务', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (init?.method === 'PATCH') return mockResponse({ ok: true, session_id: 'task-1', title: '士兰微寻访', archived: true, business_focus: null })
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) return mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: [{ role: 'assistant', content: '已有进展' }] })
      return mockResponse({ ok: true, sessions: [{ session_id: 'task-1', title: '士兰微寻访', preview: '已有进展', message_count: 1 }] })
    }))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('已有进展')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '归档任务：士兰微寻访' }))
    fireEvent.click(screen.getByRole('button', { name: '确认归档任务：士兰微寻访' }))
    await waitFor(() => expect(localStorage.getItem('asaAgentSessionId')).toBeNull())
    expect(screen.queryByText('已有进展')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
  })

  it('自然语言归档全部任务先确认，再走批量任务接口', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/copilot/sessions/archive-all')) return mockResponse({
        ok: true, archived_count: 2, session_ids: ['task-1', 'task-2'],
      })
      if (url.includes('/api/v1/copilot/sessions?')) return mockResponse({ ok: true, sessions: [
        { session_id: 'task-1', title: '士兰微寻访', preview: '继续找人', message_count: 2 },
        { session_id: 'task-2', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
      ] })
      return mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '归档右侧所有任务' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    const dialog = await screen.findByRole('alertdialog', { name: '归档全部任务' })
    expect(dialog).toHaveTextContent('消息、业务焦点与审计记录')
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/v1/copilot/stream'))).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: '确认全部归档' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).includes('/archive-all') && init?.method === 'POST')).toBe(true))
    await waitFor(() => expect(screen.queryByRole('alertdialog', { name: '归档全部任务' })).not.toBeInTheDocument())
    expect(screen.queryByText('士兰微寻访')).not.toBeInTheDocument()
    expect(screen.queryByText('长越岗位分析')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
  })

  it('任务栏归档全部按钮复用同一个确认层', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/copilot/sessions?')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({ ok: true, archived_count: 0, session_ids: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.click(await screen.findByRole('button', { name: '归档全部任务' }))
    expect(screen.getByRole('alertdialog', { name: '归档全部任务' })).toBeInTheDocument()
  })

  it('显示并可解除当前任务焦点', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/sessions?')
      ? mockResponse({ ok: true, sessions: [] })
      : mockResponse({ ok: true, session_id: 'task-1', title: '任务', archived: false, business_focus: null })))
    render(<AgentWorkspace workbench={workbench} templates={[]} context={{ type: 'job', id: 154, client: '士兰微', job: '电源专家' }}
      onOpenAnalysis={() => {}} onRunTemplate={() => {}} onManageTemplate={() => {}} onCreateTemplate={() => {}}
      onWorkbenchAction={() => {}} onOpenFullObject={() => {}} />)

    expect(await screen.findByRole('status', { name: '当前任务焦点' })).toHaveTextContent('士兰微 / 电源专家')
    fireEvent.click(screen.getByRole('button', { name: '解除任务焦点' }))
    expect(screen.queryByRole('status', { name: '当前任务焦点' })).not.toBeInTheDocument()
  })

  it('寻访结果卡把策略复盘和继续补池作为带工作流证据的 Agent 回合发送', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-result')
    const resultCard = {
      type: 'sourcing_result',
      title: '寻访结果：士兰微 · 电源专家 · 第3轮',
      context: { type: 'workflow', id: 'workflow-result-3' },
      summary: {
        workflow_id: 'workflow-result-3', round: 3, client: '士兰微', job: '电源专家', status: 'completed',
        business_outcome: 'completed_pool_insufficient', assessed_count: 50, successful_count: 47, failed_count: 3,
        total_assessed_in_job: 138,
        recommendation_breakdown: { recommended: 12, verify_first: 8, not_recommended: 27 },
        top_candidates: [],
        next_actions: [
          { type: 'discuss_strategy', label: '调整寻访策略' },
          { type: 'continue_sourcing', label: '继续补池' },
        ],
      },
    }
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-result?limit=100')) return mockResponse({
        ok: true, session_id: 'task-result',
        business_focus: { context: { type: 'workflow', id: 'workflow-result-3' }, client: '士兰微', job: { title: '电源专家' } },
        messages: [{ role: 'assistant', content: '第 3 轮寻访已完成。', action_card: resultCard }],
      })
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        'event: context\ndata: {"session_id":"task-result"}\n\nevent: done\ndata: {"ok":true,"session_id":"task-result","answer":"已受理下一步动作"}\n\n',
      )
      return mockResponse({ ok: true, sessions: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    fireEvent.click(await screen.findByRole('button', { name: '调整寻访策略' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toHaveLength(1))
    const strategyBody = JSON.parse(String(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/copilot/stream'))[0][1]?.body || '{}'))
    expect(strategyBody).toMatchObject({
      session_id: 'task-result',
      message: '请基于本轮寻访结果复盘并调整寻访策略',
      context: {
        type: 'workflow', id: 'workflow-result-3', mode: 'strategy_revision', client: '士兰微', job: '电源专家',
        sourcing_result: { round: 3, business_outcome: 'completed_pool_insufficient', assessed_count: 50, successful_count: 47, failed_count: 3 },
      },
    })

    fireEvent.click(screen.getByRole('button', { name: '继续补池' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toHaveLength(2))
    const continueBody = JSON.parse(String(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/api/v1/copilot/stream'))[1][1]?.body || '{}'))
    expect(continueBody).toMatchObject({
      session_id: 'task-result', message: '继续补池',
      context: {
        type: 'workflow', id: 'workflow-result-3', client: '士兰微',
        structured_action: {
          action_id: 'sourcing-result:workflow-result-3:continue', type: 'continue_sourcing',
          target: { type: 'workflow', id: 'workflow-result-3', client: '士兰微', label: '电源专家' },
        },
      },
    })
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

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）'), { target: { value: '推进一下' } })
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

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）'), { target: { value: '推进一下' } })
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

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）'), { target: { value: '推进一下' } })
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

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）'), { target: { value: '推进一下' } })
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
    expect(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()

    resolveRestore(mockResponse({ ok: true, session_id: 'task-1', messages: [], business_focus: null }))
    await waitFor(() => expect(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）')).toBeEnabled())
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

    fireEvent.change(screen.getByPlaceholderText('告诉 ASA 你要推进的目标…（或点上方快捷指令）'), { target: { value: '推进一下' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('正在处理：梳理岗位需求')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '正在处理：梳理岗位需求' })).toHaveClass('agent-thinking')
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

  it('恢复有更早历史的会话时提供「加载更早」入口并按 offset 翻页', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    const recent = Array.from({ length: 100 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: `历史消息 ${index + 1}` }))
    const earlier = Array.from({ length: 20 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: `更早消息 ${index + 1}` }))
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100')) {
        return mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: recent, total: 120, has_more: true })
      }
      if (url.endsWith('/api/v1/copilot/sessions/task-1?limit=100&offset=100')) {
        return mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: earlier, total: 120, has_more: false })
      }
      return mockResponse({ ok: true, sessions: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('加载更早的消息（还有 20 条）')).toBeInTheDocument()
    expect(screen.getByText('历史消息 100')).toBeInTheDocument()
    expect(screen.queryByText('更早消息 1')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '加载更早的消息（还有 20 条）' }))
    expect(await screen.findByText('更早消息 1')).toBeInTheDocument()
    expect(screen.getByText('历史消息 100')).toBeInTheDocument()
    // 最后一页加载完：入口消失。
    expect(screen.queryByRole('button', { name: /加载更早的消息/ })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(call => String(call[0]).endsWith('/api/v1/copilot/sessions/task-1?limit=100&offset=100'))).toBe(true)
  })

  it('恢复无更早历史的会话时不显示「加载更早」入口', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-1')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/copilot/sessions/task-1?limit=100')
      ? mockResponse({ ok: true, session_id: 'task-1', business_focus: null, messages: [{ role: 'assistant', content: '已恢复任务' }], total: 1, has_more: false })
      : mockResponse({ ok: true, sessions: [] })))
    renderWorkspace({ type: 'page', page: 'agent' })

    expect(await screen.findByText('已恢复任务')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /加载更早的消息/ })).not.toBeInTheDocument()
  })

  it('发送对话时附带当前页面桥接摘要', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/asa/floating/state')) return mockResponse({
        ok: true,
        active_context: {
          surface: 'liepin', source_label: '猎聘', title: '刘先生', subtitle: '中科时代 · 运动控制软件工程师',
          connected: true, job_candidate_id: 116, updated_at: '2026-08-04T13:30:00', age_seconds: 8,
        },
        active_context_raw: { surface: 'liepin', context_key: 'liepin:tab-1', instance_id: 'tab-1', job_candidate_id: 116 },
        context_quality: { stale: false },
      })
      if (url.includes('/api/v1/copilot/stream')) return streamResponse(
        'event: done\ndata: {"ok":true,"session_id":"task-bridge","answer":"收到页面上下文"}\n\n',
      )
      return mockResponse({ ok: true, sessions: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'job', id: 154, client: '长越科技', job: '自动化软件高级工程师' })

    expect(await screen.findByRole('button', { name: '显示当前页面识别' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '评估当前页面人选' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => {
      const streamCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/v1/copilot/stream'))
      expect(streamCall).toBeDefined()
      const body = JSON.parse(String(streamCall?.[1]?.body || '{}')) as { context?: Record<string, unknown> }
      expect(body.context).toMatchObject({
        type: 'job', id: 154, source: 'asa_floating', display_mode: 'workspace',
        bridge: {
          surface: 'liepin', context_key: 'liepin:tab-1', instance_id: 'tab-1',
          job_candidate_id: 116, title: '刘先生',
        },
      })
    })
  })

  it('当前任务候选人与页面候选人不同，发送前提示并阻止请求', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-candidate')
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/asa/floating/state')) return mockResponse({
        active_context: { surface: 'liepin', title: '候选人 B', job_candidate_id: 116 },
        active_context_raw: { context_key: 'liepin:tab-1', job_candidate_id: 116 },
      })
      if (url.endsWith('/api/v1/copilot/sessions/task-candidate?limit=100')) return mockResponse({
        ok: true, session_id: 'task-candidate', business_focus: { context: { type: 'candidate', id: 115 }, candidate: { name: '候选人 A' } }, messages: [],
      })
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })
    expect(await screen.findByRole('alert')).toHaveTextContent('候选人 A')
    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '评估当前人选' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/v1/copilot/stream'))).toBe(false)
  })

  it('选择继续当前任务后允许发送且不带新页面人选焦点', async () => {
    localStorage.setItem('asaAgentSessionId', 'task-candidate')
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/asa/floating/state')) return mockResponse({ active_context: { surface: 'liepin', title: '候选人 B', job_candidate_id: 116 }, active_context_raw: { context_key: 'liepin:tab-1', job_candidate_id: 116 } })
      if (url.endsWith('/api/v1/copilot/sessions/task-candidate?limit=100')) return mockResponse({ ok: true, session_id: 'task-candidate', business_focus: { context: { type: 'candidate', id: 115 }, candidate: { name: '候选人 A' } }, messages: [] })
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse('event: done\ndata: {"ok":true,"session_id":"task-candidate","answer":"已继续"}\n\n')
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace({ type: 'page', page: 'agent' })
    fireEvent.click(await screen.findByRole('button', { name: '继续当前任务' }))
    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '继续评估' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toBe(true))
    const streamCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/copilot/stream'))
    expect(JSON.parse(String(streamCall?.[1]?.body || '{}')).context).not.toHaveProperty('bridge')
  })

  it('名单点人选打开详情后，关闭详情自动恢复名单浮窗', { timeout: 30000 }, async () => {
    localStorage.clear() // 避免上一个用例留下的 asaAgentSessionId 触发旧任务恢复
    const listCard = {
      type: 'candidate_list',
      title: '岗位 137 候选名单',
      context: { type: 'job', id: 137 },
      summary: { total: 1, active: 1 },
      groups: [{ key: 'active', label: '其余可推进候选', candidates: [{ id: 1203, name: '张雯', company: 'ASM中国', title: '高级机械设计工程师', stage: '已触达' }] }],
    }
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/copilot/stream')) return streamResponse(
        `event: context\ndata: {"session_id":"task-list"}\n\nevent: done\ndata: ${JSON.stringify({ ok: true, session_id: 'task-list', answer: '名单如下', action_card: listCard })}\n\n`,
      )
      if (url.includes('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const onOpenFullObject = vi.fn()
    renderWorkspace({ type: 'page', page: 'agent' }, { onOpenFullObject })

    fireEvent.change(screen.getByLabelText('Agent 消息'), { target: { value: '给我名单' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/copilot/stream'))).toBe(true), { timeout: 5000 })
    await waitFor(() => expect(screen.queryByText('名单如下')).toBeTruthy(), { timeout: 5000 })
    // 名单自动弹出
    fireEvent.click(await screen.findByText('张雯', {}, { timeout: 8000 }))
    // 名单关闭并打开详情
    await waitFor(() => expect(screen.queryByText('张雯')).not.toBeInTheDocument())
    expect(onOpenFullObject).toHaveBeenCalledWith(expect.objectContaining({ type: 'candidate', id: 1203 }))
    // 详情关闭后名单自动恢复
    fireEvent(window, new CustomEvent(FULL_OBJECT_CLOSED_EVENT))
    expect(await screen.findByText('张雯', {}, { timeout: 8000 })).toBeInTheDocument()
    // 再点一次关闭按钮，名单正常关闭且不影响后续恢复逻辑
    fireEvent.click(screen.getByLabelText('关闭名单'))
    expect(screen.queryByText('张雯')).not.toBeInTheDocument()
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
    expect(await screen.findByRole('region', { name: '候选人决策台' })).toHaveTextContent('示例科技 · 前端工程师')
    fireEvent.click(screen.getByRole('button', { name: '复核通过' }))
    expect(await screen.findByRole('alertdialog')).toHaveTextContent('候选人将进入复核通过阶段')
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/candidate-actions/commit'))).toBe(true))
  })

  it('候选动作写入成功后不等待悬挂的详情回读', async () => {
    const detailRefresh = new Promise<Response>(() => {})
    let candidateReads = 0
    const candidateUpdated = vi.fn()
    window.addEventListener('asa:candidate-updated', candidateUpdated, { once: true })
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/candidates/1')) {
        candidateReads += 1
        return candidateReads === 1 ? mockResponse({ candidate: candidateDetail }) : detailRefresh
      }
      if (url.endsWith('/api/v1/candidate-actions/preflight')) return mockResponse({ token: 'preflight-1', impact: '候选人将进入复核通过阶段' })
      if (url.endsWith('/api/v1/candidate-actions/commit')) return mockResponse({ ok: true, stage: 'S2 复核通过' })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'candidate', id: 1, label: '张三' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开张三' }))
    await screen.findByRole('region', { name: '候选人决策台' })
    fireEvent.click(screen.getByRole('button', { name: '复核通过' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认执行' }))

    expect(await screen.findByRole('status')).toHaveTextContent('复核通过已完成。')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(candidateUpdated).toHaveBeenCalledTimes(1)
    expect((candidateUpdated.mock.calls[0][0] as CustomEvent).detail).toEqual({ id: 1, stage: 'S2 复核通过', isStopped: false })
  })

  it('候选动作成功后的详情回读失败不推翻成功回执', async () => {
    let candidateReads = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/candidates/1')) {
        candidateReads += 1
        if (candidateReads > 1) throw new Error('详情暂不可用')
        return mockResponse({ candidate: candidateDetail })
      }
      if (url.endsWith('/api/v1/candidate-actions/preflight')) return mockResponse({ token: 'preflight-1', impact: '候选人将进入复核通过阶段' })
      if (url.endsWith('/api/v1/candidate-actions/commit')) return mockResponse({ ok: true, stage: 'S2 复核通过' })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'candidate', id: 1, label: '张三' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开张三' }))
    await screen.findByRole('region', { name: '候选人决策台' })
    fireEvent.click(screen.getByRole('button', { name: '复核通过' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认执行' }))

    expect(await screen.findByRole('status')).toHaveTextContent('复核通过已完成。')
    await waitFor(() => expect(candidateReads).toBe(2))
    expect(screen.queryByText('详情暂不可用')).not.toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('候选动作写入失败时保留确认层且不广播成功事件', async () => {
    let candidateReads = 0
    const candidateUpdated = vi.fn()
    window.addEventListener('asa:candidate-updated', candidateUpdated, { once: true })
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/candidates/1')) {
        candidateReads += 1
        return mockResponse({ candidate: candidateDetail })
      }
      if (url.endsWith('/api/v1/candidate-actions/preflight')) return mockResponse({ token: 'preflight-1', impact: '候选人将进入复核通过阶段' })
      if (url.endsWith('/api/v1/candidate-actions/commit')) return mockResponse({ detail: '预检令牌已失效' }, false, 409)
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'candidate', id: 1, label: '张三' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开张三' }))
    await screen.findByRole('region', { name: '候选人决策台' })
    fireEvent.click(screen.getByRole('button', { name: '复核通过' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认执行' }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('预检令牌已失效')
    await waitFor(() => expect(candidateReads).toBe(2))
    expect(candidateUpdated).not.toHaveBeenCalled()
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

  it('工作流审批成功不等待悬挂的详情回读', async () => {
    const workflow = { ...plannedWorkflow, approvals: [{ approval_id: 'approval-1', title: '批准外部寻访', risk_level: 'R3', status: 'pending' }] }
    const detailRefresh = new Promise<Response>(() => {})
    let detailReads = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/workflows/wf-1/summary')) return mockResponse({ ok: true, workflow_id: 'wf-1', status: 'waiting_approval', progress: { completed: 1, total: 2 }, pending_approvals: [{ approval_id: 'approval-1', status: 'pending' }] })
      if (url.endsWith('/api/v1/workflows/wf-1')) {
        detailReads += 1
        return detailReads === 1 ? mockResponse(workflow) : detailRefresh
      }
      if (url.includes('/api/v1/approvals/approval-1/decision')) return mockResponse({ ok: true })
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'workflow', id: 'wf-1', label: '寻访前端工程师' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开寻访前端工程师' }))
    fireEvent.click(await screen.findByRole('button', { name: '批准本次执行' }))

    expect(await screen.findByRole('status')).toHaveTextContent('本次审批已批准，工作流已进入执行队列。')
    expect(screen.queryByRole('button', { name: '批准本次执行' })).not.toBeInTheDocument()
    expect(detailReads).toBe(2)
  })

  it('工作流启动成功不等待详情回读且保留外部审批边界', async () => {
    const detailRefresh = new Promise<Response>(() => {})
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/workflows/wf-1/summary')) return mockResponse({ ok: true, workflow_id: 'wf-1', status: 'planned', progress: { completed: 0, total: 2 }, pending_approvals: [] })
      if (url.endsWith('/api/v1/workflows/wf-1/start') && init?.method === 'POST') return mockResponse({ ok: true, workflow: { workflow_id: 'wf-1', status: 'queued' } })
      if (url.endsWith('/api/v1/workflows/wf-1')) return detailRefresh
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed
      reference={{ type: 'workflow', id: 'wf-1', label: '寻访前端工程师' }}
      workflowProgress={{ workflow_id: 'wf-1', status: 'planned', completed: 0, total: 2, label: '确认推进方案' }}
      actionCard={{ next_actions: [{ type: 'start_workflow', id: 'wf-1', label: '确认计划并准备', plan_ref: { version: 1, plan_hash: 'hash-1' } }] }}
      onOpenFull={() => {}}
    />)

    fireEvent.click(await screen.findByRole('button', { name: '确认计划并准备' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认开始' }))

    expect(await screen.findByRole('status')).toHaveTextContent('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认计划并准备' })).not.toBeInTheDocument()
    expect(screen.getByText('正在排队')).toBeInTheDocument()
  })

  it('工作流审批写入失败时保留待审批入口', async () => {
    const workflow = { ...plannedWorkflow, approvals: [{ approval_id: 'approval-1', title: '批准外部寻访', risk_level: 'R3', status: 'pending' }] }
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/workflows/wf-1/summary')) return mockResponse({ ok: true, workflow_id: 'wf-1', status: 'waiting_approval', progress: { completed: 1, total: 2 }, pending_approvals: [{ approval_id: 'approval-1', status: 'pending' }] })
      if (url.endsWith('/api/v1/workflows/wf-1')) return mockResponse(workflow)
      if (url.includes('/api/v1/approvals/approval-1/decision')) return mockResponse({ detail: '审批已过期，请刷新后重试' }, false, 409)
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'workflow', id: 'wf-1', label: '寻访前端工程师' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开寻访前端工程师' }))
    fireEvent.click(await screen.findByRole('button', { name: '批准本次执行' }))

    expect(await screen.findByText('审批已过期，请刷新后重试')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '批准本次执行' })).toBeEnabled()
    expect(screen.queryByText('本次审批已批准，工作流已进入执行队列。')).not.toBeInTheDocument()
  })

  it('工作流启动写入失败时保留确认层', async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/workflows/wf-1/summary')) return mockResponse({ ok: true, workflow_id: 'wf-1', status: 'planned', progress: { completed: 0, total: 2 }, pending_approvals: [] })
      if (url.endsWith('/api/v1/workflows/wf-1/start') && init?.method === 'POST') return mockResponse({ detail: '待确认计划版本已变化，请重新确认' }, false, 409)
      return mockResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed
      reference={{ type: 'workflow', id: 'wf-1', label: '寻访前端工程师' }}
      workflowProgress={{ workflow_id: 'wf-1', status: 'planned', completed: 0, total: 2, label: '确认推进方案' }}
      actionCard={{ next_actions: [{ type: 'start_workflow', id: 'wf-1', label: '确认计划并准备', plan_ref: { version: 1, plan_hash: 'hash-1' } }] }}
      onOpenFull={() => {}}
    />)

    fireEvent.click(await screen.findByRole('button', { name: '确认计划并准备' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认开始' }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('待确认计划版本已变化，请重新确认')
    expect(screen.getByRole('button', { name: '确认开始' })).toBeEnabled()
    expect(screen.queryByText('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')).not.toBeInTheDocument()
  })

  it('活动中的寻访工作流提供立即停止入口', async () => {
    const workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_external' },
      steps: plannedWorkflow.steps.map(step => ({ ...step, status: 'waiting_external' })),
    }
    const fetchMock = vi.fn<typeof fetch>(async input => String(input).endsWith('/wf-1/cancel')
      ? mockResponse({ ok: true, workflow: { ...workflow.workflow, status: 'cancelled' } })
      : mockResponse(workflow))
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentObjectEmbed reference={{ type: 'workflow', id: 'wf-1', label: '士兰微｜电源专家｜第3轮寻访' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开士兰微｜电源专家｜第3轮寻访' }))
    const stop = await screen.findByRole('button', { name: '结束本轮' })
    fireEvent.click(stop)
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/workflows/wf-1/cancel'))).toBe(true))
  })

  it('工作流对象卡用当前摘要覆盖会话里的旧进度', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/workflows/wf-1/summary')
      ? mockResponse({ ok: true, workflow_id: 'wf-1', status: 'completed', progress: { completed: 3, total: 3, ratio: 1 }, current_stage: '生成逐人核验点', pending_approvals: [] })
      : mockResponse(plannedWorkflow)))
    render(<AgentObjectEmbed
      reference={{ type: 'workflow', id: 'wf-1', label: '长越科技｜自动化软件高级工程师｜候选人核验' }}
      workflowProgress={{ workflow_id: 'wf-1', status: 'planned', completed: 0, total: 3, label: '锁定岗位核验范围' }}
      onOpenFull={() => {}}
    />)

    expect(await screen.findByText('已完成')).toBeInTheDocument()
    expect(screen.getByText('3 / 3 步')).toBeInTheDocument()
    expect(screen.queryByText('0 / 3 步')).not.toBeInTheDocument()
  })

  it('工作流对象卡持续同步活动状态并在完成后更新', async () => {
    vi.useFakeTimers()
    let summaryCalls = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      if (String(input).endsWith('/api/v1/workflows/wf-1/summary')) {
        summaryCalls += 1
        return summaryCalls === 1
          ? mockResponse({ ok: true, workflow_id: 'wf-1', status: 'waiting_external', progress: { completed: 2, total: 3, ratio: 0.67 }, current_stage: '执行多渠道寻访', pending_approvals: [] })
          : mockResponse({ ok: true, workflow_id: 'wf-1', status: 'completed', progress: { completed: 3, total: 3, ratio: 1 }, current_stage: '评估新增候选人', pending_approvals: [] })
      }
      return mockResponse(plannedWorkflow)
    }))
    render(<AgentObjectEmbed
      reference={{ type: 'workflow', id: 'wf-1', label: '士兰微｜电源专家｜第3轮寻访' }}
      workflowProgress={{ workflow_id: 'wf-1', status: 'waiting_external', completed: 2, total: 3, label: '执行多渠道寻访' }}
      onOpenFull={() => {}}
    />)

    await vi.waitFor(() => expect(screen.getByText('等待外部结果')).toBeInTheDocument())
    await vi.advanceTimersByTimeAsync(3000)
    await vi.waitFor(() => expect(screen.getByText('已完成')).toBeInTheDocument())
    expect(screen.getByText('3 / 3 步')).toBeInTheDocument()
    vi.useRealTimers()
  })

  it('工作流对象卡保持轻量，完整策略下沉到工作流详情', async () => {
    const workflow = {
      ...plannedWorkflow,
      steps: [
        {
          id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed', capability_id: 'search_strategy',
          output: {
            strategy: {
              channels: {
                liepin: [{ query: 'VPD 垂直供电 TLVR', purpose: '锁定服务器板级电源研发' }],
                xsaas: [{ query: 'VPD VRM 多相 Buck', purpose: '补充芯片原厂与模块电源人才' }],
              },
              review_gates: {
                hard_requirements: ['具备 VPD/VRM 实际项目证据'],
                negative_rules: ['仅有 AC/DC 经验且无板级电源项目'],
                risk_points: ['按项目深度判断资深程度'],
              },
            },
            strategy_v2: {
              step2_target_pool: [{ tier: 'T1', companies: [{ name: 'MPS' }, { name: 'Vicor' }] }],
            },
          },
        },
        plannedWorkflow.steps[1],
      ],
    }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/workflows/wf-1/summary')
      ? mockResponse({ ok: true, workflow_id: 'wf-1', status: 'waiting_approval', progress: { completed: 1, total: 2 }, current_stage: '执行多渠道寻访', pending_approvals: [] })
      : mockResponse(workflow)))
    const openFull = vi.fn()
    render(<AgentObjectEmbed reference={{ type: 'workflow', id: 'wf-1', label: '士兰微｜电源专家｜第3轮寻访' }} onOpenFull={openFull} />)

    fireEvent.click(screen.getByRole('button', { name: '展开士兰微｜电源专家｜第3轮寻访' }))
    expect(await screen.findByRole('region', { name: '执行控制台' })).toHaveTextContent('生成寻访策略')
    expect(screen.queryByText('VPD 垂直供电 TLVR')).not.toBeInTheDocument()
    expect(screen.queryByText('VPD VRM 多相 Buck')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看完整工作流' }))
    expect(openFull).toHaveBeenCalledWith(expect.objectContaining({ type: 'workflow', id: 'wf-1' }))
  })

  it('岗位对象卡使用岗位经营台而不是候选人或工作流界面', async () => {
    const job = {
      id: 9, title: '机械高级工程师', client: '长越科技', status: '已发布', priority: 'P0-最急',
      candidate_count: 18, active_candidate_count: 7, position: {}, profile: {}, candidates: [], stages: [], search_experiments: [], events: [], followups: [],
      funnel: { total: 18, active: 7, stopped: 4, contacted: 6, recommended: 2 }, hard_requirements: '固晶机、共晶机或键合机整机机械经验',
    }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).endsWith('/api/v1/jobs/9') ? mockResponse({ job }) : mockResponse({})))
    render(<AgentObjectEmbed reference={{ type: 'job', id: 9, label: '机械高级工程师', subtitle: '长越科技' }} onOpenFull={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '展开机械高级工程师' }))
    const console = await screen.findByRole('region', { name: '岗位经营台' })
    expect(console).toHaveTextContent('18全部7推进中6已触达2已推荐')
    expect(console).toHaveTextContent('固晶机、共晶机或键合机整机机械经验')
    expect(screen.queryByRole('region', { name: '候选人决策台' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '执行控制台' })).not.toBeInTheDocument()
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
