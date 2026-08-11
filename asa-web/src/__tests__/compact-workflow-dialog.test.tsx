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
    const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/v1/workflows/wf-1/start'))
    expect(call).toBeDefined()
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toMatchObject({
      expected_plan_version: 1,
      expected_plan_hash: 'plan-hash-1',
    })
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
})
