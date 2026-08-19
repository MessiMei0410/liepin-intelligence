import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Workflow } from '../api'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { stepBusinessResult } from '../workflows/utils'
import { mockResponse, plannedWorkflow } from './helpers'
import type { SourcingResultCardData } from '../workflows/SourcingResultCard'

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

  it('渠道命中风险停机时展示明确原因和停止范围', () => {
    const result = stepBusinessResult({
      id: 2,
      sequence: 2,
      business_label: '执行多渠道寻访',
      risk_level: 'R3',
      status: 'completed',
      output: {
        external_result: {
          channel_risk_stop: {
            active: true,
            channel: 'liepin',
            signal: '安全风险',
            message: '猎聘命中安全风险提示，已停止猎聘及后续分页。',
          },
        },
      },
    })
    expect(result.facts).toContain('猎聘命中安全风险提示，已停止猎聘及后续分页。')
  })

  it('blocked + completed_pool_insufficient → 显示业务文案与 Agent 下一步入口，不出现英文枚举', () => {
    const { container } = renderPanel(outcomeWorkflow('blocked', 'completed_pool_insufficient', { steps: finishedSteps }))
    expect(screen.getAllByText('本轮完成，合格人数不足').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '复核现有人选' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '在 Agent 中调整策略' })).toBeInTheDocument()
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
    expect(screen.queryByRole('button', { name: '在 Agent 中调整策略' })).not.toBeInTheDocument()
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

  it('在 Agent 中调整策略 → 站内附着工作流上下文并保留详情', async () => {
    const user = userEvent.setup()
    const close = vi.fn()
    const contexts: unknown[] = []
    window.addEventListener('asa:open-agent', event => contexts.push((event as CustomEvent).detail), { once: true })
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    render(<WorkflowPanel value={outcomeWorkflow('blocked', 'completed_needs_review', { steps: finishedSteps })} jobs={[]} close={close} reload={vi.fn()} openCandidate={() => undefined} archived={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '在 Agent 中调整策略' }))
    expect(contexts).toEqual([expect.objectContaining({ type: 'workflow', id: 'wf-r3', mode: 'strategy_revision' })])
    expect(close).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: '寻访前端工程师' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/v1/workflows/wf-r3/revise'))).toBe(false)
  })

  it('结束本轮 → 确认卡确认后触发既有 archive action 并回调 archived', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const { archived } = renderPanel(outcomeWorkflow('blocked', 'completed_pool_insufficient', { steps: finishedSteps }))
    await user.click(screen.getByRole('button', { name: '结束本轮' }))
    // P7 确认链：归档先弹确认卡，确认后才发 archive 写请求
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('归档工作流')
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/v1/workflows/wf-r3/archive'))).toBe(false)
    await user.click(screen.getByRole('button', { name: '确认归档' }))
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

  it('候选人核验工作流完成后展示评估结果标题，不再等待寻访渠道', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/workflows/wf-r3/candidates')
      ? mockResponse({
        ok: true,
        items: [
          { id: 101, name: '刘**', company: '中科时代计算机系统有限公司', title: '运动控制软件工程师', fit_score: 100, fit_level: 'A-优先推进', recommendation: 'recommended', assessed: true },
          { id: 102, name: '王**', company: '示例科技', title: '资深软件工程师', fit_score: 95, fit_level: 'A-优先推进', assessed: true },
        ],
        total: 116,
      })
      : mockResponse({ ok: true })))
    const steps: Workflow['steps'] = [
      { id: 1, sequence: 1, business_label: '锁定岗位核验范围', risk_level: 'R0', status: 'completed' },
      { id: 2, sequence: 2, business_label: '整理候选人核验队列', risk_level: 'R0', status: 'completed' },
      {
        id: 3,
        sequence: 3,
        business_label: '生成逐人核验点',
        risk_level: 'R1',
        status: 'completed',
        capability_id: 'candidate_batch_assessment',
        output: { assessment_queue: { completed: 116, started: 15, completed_items: [] } },
      },
    ]

    renderPanel(outcomeWorkflow('completed', null, { steps }))

    expect(await screen.findByText('本轮评估 15 位 · 岗位已评估 116 人')).toBeInTheDocument()
    expect(screen.queryByText('等待渠道与评估结果')).not.toBeInTheDocument()
    expect(screen.getByText('刘**')).toBeInTheDocument()
    expect(screen.getByText('王**')).toBeInTheDocument()
    expect(screen.getByText('推荐')).toBeInTheDocument()
    expect(screen.getByText('待复核')).toBeInTheDocument()
    expect(screen.getByText('候选人核验结果')).toBeInTheDocument()
  })

  it('completed 且存在 sourcing_result artifact 时自动弹出寻访结果卡', async () => {
    const resultCard: SourcingResultCardData = {
      type: 'sourcing_result',
      title: '寻访结果：ACME · 前端工程师 · 第1轮',
      context: { type: 'workflow', id: 'wf-r3' },
      summary: {
        workflow_id: 'wf-r3',
        round: 1,
        client: 'ACME',
        job: '前端工程师',
        status: 'completed',
        business_outcome: null,
        assessed_count: 50,
        successful_count: 47,
        failed_count: 3,
        total_assessed_in_job: 138,
        recommendation_breakdown: { recommended: 12, verify_first: 8, not_recommended: 27 },
        top_candidates: [],
        next_actions: [
          { type: 'review_candidates', label: '复核现有人选' },
          { type: 'discuss_strategy', label: '调整寻访策略' },
          { type: 'archive', label: '结束本轮' },
        ],
      },
    }
    renderPanel(outcomeWorkflow('completed', null, {
      steps: finishedSteps,
      artifacts: [{
        artifact_id: 'artifact-sourcing-result',
        title: '寻访结果：ACME · 前端工程师 · 第1轮',
        artifact_type: 'sourcing_result',
        validation_status: 'passed',
        mime_type: 'application/json',
        metadata: { action_card: resultCard },
      } as unknown as Workflow['artifacts'][number]],
    }))
    expect(await screen.findByRole('dialog', { name: resultCard.title })).toBeInTheDocument()
    expect(screen.getByText('50')).toBeInTheDocument()
    expect(screen.getByText('47')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '复核现有人选' })).toBeInTheDocument()
  })
})
