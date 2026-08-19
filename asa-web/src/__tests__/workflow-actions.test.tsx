import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

const renderPanel = (reload = vi.fn()) => {
  render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)
  return { reload }
}

describe('工作流动作反馈', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('点击启动后按钮立即进入 loading，写入成功后采用回执并后台刷新', async () => {
    let release: (value: Response) => void = () => undefined
    // R7：动作成功后先打 summary 再按需 reload；summary 调用立即应答，动作调用仍由 release 控制以覆盖 in-flight 状态。
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      void init
      return String(input).includes('/summary')
        ? Promise.resolve(mockResponse({ ok: true }))
        : new Promise<Response>(resolve => { release = resolve })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const { reload } = renderPanel()
    const start = screen.getByRole('button', { name: '确认计划并准备' })
    await user.click(start)
    expect(start).toBeDisabled()
    expect(screen.getByRole('button', { name: '立即停止寻访' })).toBeDisabled()
    release(mockResponse({ ok: true }))
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(await screen.findByRole('status')).toHaveTextContent('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')
    expect(screen.queryByRole('button', { name: '确认计划并准备' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '暂停寻访' })).toBeEnabled()
    const startCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/wf-1/start'))
    const body = JSON.parse(String((startCall?.[1] as RequestInit).body))
    expect(body).toMatchObject({ expected_plan_version: 1, expected_plan_hash: 'plan-hash-1' })
  })

  it('启动成功不等待悬挂详情回读，且优先采用 Core 返回的真实工作流状态', async () => {
    const reload = vi.fn(() => new Promise<void>(() => undefined))
    const waitingApproval = {
      ...plannedWorkflow,
      goal: { ...plannedWorkflow.goal, status: 'waiting_approval' },
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_approval', updated_at: '2026-08-14 10:00:00' },
      approvals: [{
        approval_id: 'approval-live',
        title: '执行多渠道寻访',
        risk_level: 'R3',
        status: 'pending',
        created_at: '2026-08-14 10:00:00',
      }],
    }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/workflows/wf-1/start')) return mockResponse(waitingApproval)
      if (url.includes('/candidates')) return mockResponse({ ok: true, items: [], total: 0 })
      if (url.includes('/steps/')) return mockResponse({ ok: true, step: waitingApproval.steps[0] })
      return mockResponse({ ok: true })
    }))
    renderPanel(reload)

    await userEvent.click(screen.getByRole('button', { name: '确认计划并准备' }))

    expect(await screen.findByRole('status')).toHaveTextContent('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')
    expect(screen.getAllByText('等待审批').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '批准本次外部寻访' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '确认计划并准备' })).not.toBeInTheDocument()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('接口失败时显示可读错误文案且按钮恢复可用', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse({ detail: '工作流已在运行中' }, false, 409)))
    const user = userEvent.setup()
    renderPanel()
    const start = screen.getByRole('button', { name: '确认计划并准备' })
    await user.click(start)
    expect(await screen.findByText('工作流已在运行中')).toBeInTheDocument()
    expect(start).toBeEnabled()
  })

  it('外部寻访暂停成功不等待悬挂回读，并立即提供继续或停止', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const reload = vi.fn(() => new Promise<void>(() => undefined))
    const waitingExternal = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_external' },
      goal: { ...plannedWorkflow.goal, status: 'waiting_external' },
      steps: plannedWorkflow.steps.map(step => ({ ...step, status: step.id === 1 ? 'completed' : 'waiting_external' })),
    }
    render(<WorkflowPanel value={waitingExternal} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)

    await user.click(screen.getByRole('button', { name: '暂停寻访' }))
    // P7 确认链：先弹确认卡，填原因（preflight 必填）并确认后才真正触发写链路
    const pauseDialog = await screen.findByRole('alertdialog')
    expect(pauseDialog).toHaveTextContent('渠道会在当前查询单元结束后停止')
    await user.type(screen.getByRole('textbox', { name: '原因说明' }), '客户要求暂停一周')
    await user.click(screen.getByRole('button', { name: '确认暂停' }))
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/workflows/wf-1/pause'))).toBe(true)
    expect(await screen.findByRole('status')).toHaveTextContent('已请求暂停寻访，渠道会在当前查询单元结束后停止。')
    expect(screen.getByText('已暂停，渠道会在当前查询单元结束后停止。')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '继续寻访' }))
    await user.type(await screen.findByRole('textbox', { name: '原因说明' }), '客户反馈已到位')
    await user.click(screen.getByRole('button', { name: '确认继续' }))
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/workflows/wf-1/resume'))).toBe(true)
    expect(screen.getByRole('button', { name: '立即停止寻访' })).toBeInTheDocument()
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('R3 审批成功不等待悬挂回读，并立即移除已消费审批', async () => {
    const reload = vi.fn(() => new Promise<void>(() => undefined))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    const value = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_approval' },
      goal: { ...plannedWorkflow.goal, status: 'waiting_approval' },
      approvals: [{
        approval_id: 'approval-sourcing',
        title: '执行多渠道寻访',
        risk_level: 'R3',
        status: 'pending',
        created_at: '2026-08-14 10:00:00',
      }],
    }
    render(<WorkflowPanel value={value} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)

    await userEvent.click(screen.getByRole('button', { name: '批准本次外部寻访' }))

    expect(await screen.findByRole('status')).toHaveTextContent('本次审批已批准，工作流已进入执行队列。')
    expect(screen.queryByRole('button', { name: '批准本次外部寻访' })).not.toBeInTheDocument()
    expect(screen.getByText('当前没有待审批动作')).toBeInTheDocument()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('失败步骤重试成功不等待悬挂回读，并立即移除重复重试入口', async () => {
    const reload = vi.fn(() => new Promise<void>(() => undefined))
    const value = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'failed' },
      goal: { ...plannedWorkflow.goal, status: 'failed' },
      steps: [{ id: 9, sequence: 1, business_label: '执行渠道寻访', risk_level: '中', status: 'failed', error: '渠道连接中断' }],
      progress: { completed: 0, total: 1, ratio: 0 },
    }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/candidates')) return mockResponse({ ok: true, items: [], total: 0 })
      if (url.includes('/steps/')) return mockResponse({ ok: true, step: value.steps[0] })
      return mockResponse({ ok: true })
    }))
    render(<WorkflowPanel value={value} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)

    await userEvent.click(screen.getByRole('button', { name: '重试此步骤' }))

    expect(await screen.findByRole('status')).toHaveTextContent('重试请求已提交，失败步骤已重新进入执行队列。')
    expect(screen.queryByRole('button', { name: '重试此步骤' })).not.toBeInTheDocument()
    expect(screen.getAllByText('正在排队').length).toBeGreaterThan(0)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('R3 寻访审批展示检索轴、评估约束和非平台筛选语义', () => {
    const value = {
      ...plannedWorkflow,
      approvals: [{
        approval_id: 'approval-sourcing',
        title: '执行多渠道寻访',
        risk_level: 'R3',
        status: 'pending',
        created_at: '2026-07-30 10:00:00',
        preflight: {
          channel: 'Liepin + X-SaaS',
          query_plan_v1: {
            cell_count: 163,
            dimensions: {
              locations: ['杭州'],
              levels: ['资深工程师'],
              scenarios: ['运动控制'],
            },
            execution_semantics: {
              retrieval_axes: ['channel', 'query'],
              platform_filters: [],
            },
          },
        },
      }],
    }

    render(<WorkflowPanel value={value} jobs={[]} close={() => undefined} reload={() => undefined} openCandidate={() => undefined} archived={() => undefined} />)

    const scope = screen.getByLabelText('寻访执行范围')
    expect(scope).toHaveTextContent('渠道 + 关键词（163 个查询单元）')
    expect(scope).toHaveTextContent('地点：杭州')
    expect(scope).toHaveTextContent('职级：资深工程师')
    expect(scope).toHaveTextContent('场景：运动控制')
    expect(scope).toHaveTextContent('不作为平台筛选')
    expect(screen.getByRole('button', { name: '批准本次外部寻访' })).toBeInTheDocument()
  })
})
