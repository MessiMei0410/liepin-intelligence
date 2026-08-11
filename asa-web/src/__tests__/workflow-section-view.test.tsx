import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Workflow } from '../api'
import { WorkflowSectionView, type WorkflowSection } from '../workflows/WorkflowSectionView'
import { mockResponse, plannedWorkflow } from './helpers'

const sectionWorkflow: Workflow = {
  ...plannedWorkflow,
  steps: [
    {
      id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed',
      capability_id: 'search_strategy',
      output: {
        strategy: {
          client: 'ACME', job: '前端工程师',
          channels: { liepin: [{ name: '核心组', queries: ['react 前端'] }], xsaas: [] },
          review_gates: {},
        },
      },
    },
    { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'completed', capability_id: 'multi_channel_sourcing' },
    { id: 3, sequence: 3, business_label: '候选人批量核验', risk_level: '低', status: 'pending', capability_id: 'candidate_batch_assessment' },
  ],
  events: [{ id: 1, event_type: 'step', status: 'completed', summary: '策略生成完成', created_at: '2026-08-11 10:00:00' }],
  artifacts: [{ artifact_id: 'art-1', title: '寻访纪要', artifact_type: 'sourcing_result', validation_status: 'passed' }],
}

const renderView = (section: WorkflowSection, overrides: {
  value?: Workflow
  back?: () => void
  close?: () => void
  reload?: () => void | Promise<void>
  openCandidate?: (id: number) => void
  openFull?: () => void
} = {}) => render(<WorkflowSectionView
  value={overrides.value || sectionWorkflow}
  jobs={[]}
  section={section}
  back={overrides.back || vi.fn()}
  close={overrides.close || vi.fn()}
  reload={overrides.reload || vi.fn()}
  openCandidate={overrides.openCandidate || vi.fn()}
  openFull={overrides.openFull || vi.fn()}
/>)

describe('工作流模块二级界面', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('策略界面只挂载策略模块，不发人选/漏斗请求', () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderView('strategy')

    expect(screen.getByRole('dialog', { name: `寻访策略：${sectionWorkflow.goal.title}` })).toBeInTheDocument()
    expect(container.querySelector('.workflow-strategy')).toBeInTheDocument()
    expect(container.querySelector('.workflow-candidates')).not.toBeInTheDocument()
    expect(container.querySelector('.workflow-funnel')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('人选界面只挂载人选名单，并按需拉取人选分页', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/candidates')) {
        return mockResponse({ ok: true, items: [{ id: 11, name: '李雷', company: 'ACME', title: '前端工程师' }], total: 1 })
      }
      return mockResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderView('candidates')

    expect(screen.getByRole('dialog', { name: `人选名单：${sectionWorkflow.goal.title}` })).toBeInTheDocument()
    expect(container.querySelector('.workflow-candidates')).toBeInTheDocument()
    expect(container.querySelector('.workflow-strategy')).not.toBeInTheDocument()
    expect(container.querySelector('.workflow-funnel')).not.toBeInTheDocument()
    expect(await screen.findByText('李雷')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/sourcing-funnel'))).toBe(false)
  })

  it('漏斗界面只挂载渠道漏斗，并按需拉取漏斗明细', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/sourcing-funnel')) return mockResponse({ ok: true, channels: [], runs: [] })
      return mockResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderView('funnel')

    expect(container.querySelector('.workflow-funnel')).toBeInTheDocument()
    expect(container.querySelector('.workflow-candidates')).not.toBeInTheDocument()
    expect(await screen.findAllByText('该轮未记录渠道明细', { exact: false })).not.toHaveLength(0)
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/candidates'))).toBe(false)
  })

  it('动态界面渲染执行动态，不发任何附加请求', () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderView('events')

    expect(screen.getAllByText('执行动态').length).toBeGreaterThan(0)
    expect(screen.getByText('策略生成完成')).toBeInTheDocument()
    expect(container.querySelector('.workflow-strategy')).not.toBeInTheDocument()
    expect(container.querySelector('.workflow-candidates')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('产物界面列出产物，点击打开产物详情弹窗', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/artifacts/art-1')) {
        return mockResponse({
          ok: true,
          artifact: {
            artifact_id: 'art-1', artifact_type: 'sourcing_result', title: '寻访纪要',
            mime_type: 'text/plain', content: '本轮寻访交付 3 人。', content_size: 12,
            content_truncated: false, metadata: {}, validation_status: 'passed',
            downloadable: false, download_kind: 'none', file_name: '', download_url: '',
          },
        })
      }
      return mockResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderView('artifacts')

    expect(screen.getAllByText('结果与产物').length).toBeGreaterThan(0)
    expect(container.querySelector('.workflow-candidates')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '查看产物：寻访纪要' }))
    expect(await screen.findByText('本轮寻访交付 3 人。')).toBeInTheDocument()
  })

  it('返回键回到摘要，底栏可进入完整详情', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ok: true })))
    const back = vi.fn()
    const openFull = vi.fn()
    renderView('strategy', { back, openFull })

    await userEvent.click(screen.getByRole('button', { name: '返回' }))
    expect(back).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: '查看完整详情' }))
    expect(openFull).toHaveBeenCalledTimes(1)
  })

  it('活跃工作流保持摘要轮询同步', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/summary')) {
        return mockResponse({ ok: true, status: 'running', progress: { completed: 1, total: 3 }, pending_approvals: [], recent_events: [] })
      }
      return mockResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const reload = vi.fn()
    const live: Workflow = {
      ...sectionWorkflow,
      workflow: { ...sectionWorkflow.workflow, status: 'running' },
    }
    renderView('strategy', { value: live, reload })

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/summary'))).toBe(true), { timeout: 4000 })
  })
})
