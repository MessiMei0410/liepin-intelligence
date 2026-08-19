import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Workflow } from '../api'
import { CompactWorkflowDialog } from '../workflows/CompactWorkflowDialog'
import { mockResponse, plannedWorkflow } from './helpers'

const renderDialog = (value: Workflow = plannedWorkflow, overrides: {
  close?: () => void
  reload?: () => void | Promise<void>
  archived?: () => void
  openDetail?: (section: 'strategy' | 'candidates' | 'funnel' | 'events' | 'artifacts' | 'full') => void
} = {}) => render(<CompactWorkflowDialog
  value={value}
  close={overrides.close || vi.fn()}
  reload={overrides.reload || vi.fn()}
  archived={overrides.archived || vi.fn()}
  openDetail={overrides.openDetail || vi.fn()}
/>)

describe('轻量工作流浮层', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('默认只显示步骤、进度和必要操作，不挂载重内容', async () => {
    const openDetail = vi.fn()
    const value: Workflow = {
      ...plannedWorkflow,
      progress: { completed: 1, total: 7, ratio: 1 / 7 },
      steps: [
        { id: 1, sequence: 1, business_label: '已完成步骤', risk_level: '低', status: 'completed' },
        { id: 2, sequence: 2, business_label: '执行中步骤', risk_level: '低', status: 'running' },
        { id: 3, sequence: 3, business_label: '待审批步骤', risk_level: 'R3', status: 'waiting_approval' },
        { id: 4, sequence: 4, business_label: '未开始步骤', risk_level: '低', status: 'pending' },
        { id: 5, sequence: 5, business_label: '失败步骤', risk_level: '中', status: 'failed', error: '渠道连接失败' },
        { id: 6, sequence: 6, business_label: '暂停步骤', risk_level: '低', status: 'paused' },
        { id: 7, sequence: 7, business_label: '取消步骤', risk_level: '低', status: 'cancelled' },
      ],
    }
    const { container } = renderDialog(value, { openDetail })

    expect(screen.getByRole('dialog', { name: `工作流：${value.goal.title}` })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(7)
    expect(container.querySelector('[data-step-status="completed"] .compact-step-icon')).toBeInTheDocument()
    expect(container.querySelector('[data-step-status="running"] .spin')).toBeInTheDocument()
    expect(container.querySelector('[data-step-status="waiting_approval"] .pulse')).toBeInTheDocument()
    expect(container.querySelector('[data-step-status="failed"]')).toHaveTextContent('失败')
    expect(screen.getByLabelText('工作流进度：1/7 步')).toHaveTextContent('第 2 / 7 步')
    expect(container.querySelector('.workflow-strategy')).not.toBeInTheDocument()
    expect(container.querySelector('.workflow-candidates')).not.toBeInTheDocument()
    expect(container.querySelector('.workflow-funnel')).not.toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '查看' }))
    await user.click(screen.getByRole('menuitem', { name: '寻访策略' }))
    expect(openDetail).toHaveBeenCalledWith('strategy')
  })

  it('确认计划沿用 plan_ref，并在成功后刷新', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    const reload = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    renderDialog(plannedWorkflow, { reload })

    await userEvent.click(screen.getByRole('button', { name: '确认计划并准备' }))
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('button', { name: '确认计划并准备' })).not.toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/workflows/wf-1/start'))
    expect(call).toBeDefined()
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toMatchObject({
      expected_plan_version: 1,
      expected_plan_hash: 'plan-hash-1',
    })
  })

  it('确认计划成功不等待悬挂回读，并保留 R3 外部审批边界', async () => {
    const reload = vi.fn(() => new Promise<void>(() => {}))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    renderDialog(plannedWorkflow, { reload })

    await userEvent.click(screen.getByRole('button', { name: '确认计划并准备' }))

    expect(await screen.findByRole('status')).toHaveTextContent('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')
    expect(screen.queryByRole('button', { name: '确认计划并准备' })).not.toBeInTheDocument()
    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('确认计划成功后的回读失败不推翻成功回执', async () => {
    const reload = vi.fn(() => Promise.reject(new Error('详情暂不可用')))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    renderDialog(plannedWorkflow, { reload })

    await userEvent.click(screen.getByRole('button', { name: '确认计划并准备' }))

    expect(await screen.findByRole('status')).toHaveTextContent('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('详情暂不可用')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('确认计划写入失败时保留启动入口', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ detail: '计划版本已变化，请刷新后重试' }, false, 409)))
    renderDialog(plannedWorkflow)

    await userEvent.click(screen.getByRole('button', { name: '确认计划并准备' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('计划版本已变化，请刷新后重试')
    expect(screen.getByRole('button', { name: '确认计划并准备' })).toBeEnabled()
  })

  it('待审批动作留在首层，并复用原审批接口', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    const reload = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_approval' },
      approvals: [{ approval_id: 'approval-1', title: '执行多渠道寻访', risk_level: 'R3', status: 'pending', created_at: '2026-08-11 10:00:00' }],
    }
    renderDialog(value, { reload })

    expect(screen.getByRole('region', { name: '待审批操作' })).toHaveTextContent('执行多渠道寻访')
    await userEvent.click(screen.getByRole('button', { name: '批准本次寻访' }))
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/v1/approvals/approval-1/decision'))).toBe(true)
  })

  it('审批成功不等待悬挂回读并立即移除已消费入口', async () => {
    const reload = vi.fn(() => new Promise<void>(() => {}))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_approval' },
      approvals: [{ approval_id: 'approval-1', title: '执行多渠道寻访', risk_level: 'R3', status: 'pending', created_at: '2026-08-11 10:00:00' }],
    }
    renderDialog(value, { reload })

    await userEvent.click(screen.getByRole('button', { name: '批准本次寻访' }))

    expect(await screen.findByRole('status')).toHaveTextContent('本次审批已批准，工作流已进入执行队列。')
    expect(screen.queryByRole('region', { name: '待审批操作' })).not.toBeInTheDocument()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('审批写入失败时保留待审批入口', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ detail: '审批已过期，请刷新后重试' }, false, 409)))
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'waiting_approval' },
      approvals: [{ approval_id: 'approval-1', title: '执行多渠道寻访', risk_level: 'R3', status: 'pending', created_at: '2026-08-11 10:00:00' }],
    }
    renderDialog(value)

    await userEvent.click(screen.getByRole('button', { name: '批准本次寻访' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('审批已过期，请刷新后重试')
    expect(screen.getByRole('button', { name: '批准本次寻访' })).toBeEnabled()
  })

  it('暂停成功不等待悬挂回读，并立即切换为继续入口', async () => {
    const reload = vi.fn(() => new Promise<void>(() => {}))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'running', started_at: '2026-08-14 08:00:00' },
      steps: [{ id: 1, sequence: 1, business_label: '执行渠道寻访', risk_level: '中', status: 'running' }],
      progress: { completed: 0, total: 1, ratio: 0 },
    }
    renderDialog(value, { reload })
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: '更多工作流操作' }))
    await user.click(screen.getByRole('menuitem', { name: '暂停寻访' }))

    // P7 确认链：菜单点击只打开确认卡，填原因并确认后才执行
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('暂停寻访')
    await user.type(screen.getByRole('textbox', { name: '原因说明' }), '客户要求暂停')
    await user.click(screen.getByRole('button', { name: '确认暂停' }))

    expect(await screen.findByRole('status')).toHaveTextContent('已请求暂停寻访，渠道会在当前查询单元结束后停止。')
    await user.click(screen.getByRole('button', { name: '更多工作流操作' }))
    expect(screen.getByRole('menuitem', { name: '继续寻访' })).toBeEnabled()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('失败步骤可直接重试并显示接口错误', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ detail: '渠道仍不可用' }, false, 409)))
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'failed' },
      steps: [{ id: 9, sequence: 1, business_label: '执行渠道寻访', risk_level: '中', status: 'failed', error: '连接中断' }],
      progress: { completed: 0, total: 1, ratio: 0 },
    }
    renderDialog(value)

    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('渠道仍不可用')
  })

  it('重试成功不等待悬挂回读并立即移除失败入口', async () => {
    const reload = vi.fn(() => new Promise<void>(() => {}))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'failed' },
      steps: [{ id: 9, sequence: 1, business_label: '执行渠道寻访', risk_level: '中', status: 'failed', error: '连接中断' }],
      progress: { completed: 0, total: 1, ratio: 0 },
    }
    renderDialog(value, { reload })

    await userEvent.click(screen.getByRole('button', { name: '重试' }))

    expect(await screen.findByRole('status')).toHaveTextContent('重试请求已提交，失败步骤已重新进入执行队列。')
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
    expect(screen.getByRole('listitem')).toHaveAttribute('data-step-status', 'queued')
    expect(screen.getByRole('listitem')).toHaveTextContent('排队中')
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('重试后服务端记录新的失败版本时恢复真实失败入口', async () => {
    const reload = vi.fn(() => new Promise<void>(() => {}))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    const value: Workflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'failed' },
      steps: [{ id: 9, sequence: 1, business_label: '执行渠道寻访', risk_level: '中', status: 'failed', error: '首次连接中断', updated_at: '2026-08-14 08:00:00' }],
      progress: { completed: 0, total: 1, ratio: 0 },
    }
    const view = renderDialog(value, { reload })

    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByRole('listitem')).toHaveAttribute('data-step-status', 'queued')

    view.rerender(<CompactWorkflowDialog
      value={{
        ...value,
        steps: [{ ...value.steps[0], error: '重试后仍然连接失败', updated_at: '2026-08-14 08:01:00' }],
      }}
      close={vi.fn()}
      reload={reload}
      archived={vi.fn()}
      openDetail={vi.fn()}
    />)

    expect(screen.getByRole('listitem')).toHaveAttribute('data-step-status', 'failed')
    expect(screen.getByRole('button', { name: '重试' })).toBeEnabled()
    expect(screen.getByText('重试后仍然连接失败')).toBeInTheDocument()
  })
})
