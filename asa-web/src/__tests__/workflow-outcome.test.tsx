import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Workflow } from '../api'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

const outcomeWorkflow = (status: string, businessOutcome: string | null, overrides: Partial<Workflow> = {}): Workflow => ({
  ...plannedWorkflow,
  business_outcome: businessOutcome,
  goal: { ...plannedWorkflow.goal, status, business_outcome: businessOutcome },
  workflow: { workflow_id: 'wf-r3', status, business_outcome: businessOutcome },
  progress: { completed: 2, total: 2, ratio: 1 },
  ...overrides,
})

const finishedSteps: Workflow['steps'] = [
  { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed' },
  { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'completed' },
]

const failedSteps: Workflow['steps'] = [
  { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed' },
  { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'failed', error: 'Traceback (most recent call last)' },
]

const renderPanel = (value: Workflow) => {
  const archived = vi.fn()
  const utils = render(<WorkflowPanel value={value} jobs={[]} close={() => undefined} reload={vi.fn()} openCandidate={() => undefined} archived={archived} />)
  return { archived, ...utils }
}

describe('R3 工作流业务终态', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('blocked + completed_pool_insufficient → 显示业务文案与三个下一步按钮，不出现英文枚举', () => {
    const { container } = renderPanel(outcomeWorkflow('blocked', 'completed_pool_insufficient', { steps: finishedSteps }))
    expect(screen.getAllByText('本轮完成，合格人数不足').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '复核现有人选' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '调整条件再搜' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '结束本轮' })).toBeInTheDocument()
    expect(container).not.toHaveTextContent('completed_pool_insufficient')
    expect(container.querySelector('.workflow-progress')?.className).toContain('needs-approval')
  })

  it('blocked + completed_needs_review → 有待复核人选文案（amber）与三个按钮', () => {
    renderPanel(outcomeWorkflow('blocked', 'completed_needs_review', { steps: finishedSteps }))
    expect(screen.getAllByText('本轮完成，合格人数不足，有待复核人选').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '复核现有人选' })).toBeInTheDocument()
  })

  it('failed → 红色技术失败，附失败步骤名，无下一步按钮', () => {
    const { container } = renderPanel(outcomeWorkflow('failed', null, { steps: failedSteps }))
    expect(screen.getAllByText('技术失败：执行多渠道寻访').length).toBeGreaterThan(0)
    expect(container.querySelector('.workflow-progress')?.className).toContain('error')
    expect(screen.queryByRole('button', { name: '复核现有人选' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '调整条件再搜' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '结束本轮' })).not.toBeInTheDocument()
  })

  it('blocked + business_outcome null → 流程阻塞，待处理（非红色），无下一步按钮', () => {
    const { container } = renderPanel(outcomeWorkflow('blocked', null, { steps: finishedSteps }))
    expect(screen.getAllByText('流程阻塞，待处理').length).toBeGreaterThan(0)
    const progress = container.querySelector('.workflow-progress')?.className || ''
    expect(progress).not.toContain('error')
    expect(progress).not.toContain('needs-approval')
    expect(screen.queryByRole('button', { name: '复核现有人选' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '结束本轮' })).not.toBeInTheDocument()
  })

  it('调整条件再搜 → 打开既有修改计划对话框（同链路 revise）', async () => {
    const user = userEvent.setup()
    renderPanel(outcomeWorkflow('blocked', 'completed_needs_review', { steps: finishedSteps }))
    await user.click(screen.getByRole('button', { name: '调整条件再搜' }))
    expect(await screen.findByRole('dialog', { name: '修改计划' })).toBeInTheDocument()
  })

  it('结束本轮 → 触发既有 archive action 并回调 archived', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const { archived } = renderPanel(outcomeWorkflow('blocked', 'completed_pool_insufficient', { steps: finishedSteps }))
    await user.click(screen.getByRole('button', { name: '结束本轮' }))
    await waitFor(() => expect(archived).toHaveBeenCalledTimes(1))
    const archiveCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/v1/workflows/wf-r3/archive'))
    expect(archiveCall).toBeDefined()
    expect((archiveCall?.[1] as RequestInit).method).toBe('POST')
  })

  it('复核现有人选 → 滚动到候选人结果视图', async () => {
    const scrollSpy = vi.fn()
    const original = window.HTMLElement.prototype.scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = scrollSpy
    try {
      const user = userEvent.setup()
      renderPanel(outcomeWorkflow('blocked', 'completed_needs_review', { steps: finishedSteps }))
      expect(screen.getByText('人选结果')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: '复核现有人选' }))
      expect(scrollSpy).toHaveBeenCalledTimes(1)
    } finally {
      window.HTMLElement.prototype.scrollIntoView = original
    }
  })
})
