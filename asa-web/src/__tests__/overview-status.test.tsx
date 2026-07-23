import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Overview } from '../pages/Overview'
import type { Dashboard } from '../api'

// T1：dashboard workflows[] 透传 business_outcome 后，总览工作流标签统一走 statusMapping
const dashboardWith = (workflow: Record<string, unknown>): Dashboard => ({
  ok: true,
  counts: { active_jobs: 1, candidates: 1, pending_candidates: 1, pending_approvals: 0 },
  workflows: [{ workflow_id: 'wf-1', status: 'blocked', current_stage: 'assessment', title: '士兰微｜技术市场经理｜第3轮寻访', ...workflow } as Dashboard['workflows'] extends Array<infer W> ? W : never],
  recent_events: [],
})

const renderOverview = (dashboard: Dashboard) =>
  render(<Overview dashboard={dashboard} jobs={[]} candidates={[]} openWorkflow={() => {}} openCandidate={() => {}} archiveWorkflow={() => {}} />)

describe('Overview 工作流状态标签（T1）', () => {
  it('blocked + completed_needs_review → 业务文案，不显示"已阻塞"', () => {
    renderOverview(dashboardWith({ business_outcome: 'completed_needs_review' }))
    expect(screen.getByText('本轮完成，合格人数不足，有待复核人选')).toBeInTheDocument()
    expect(screen.queryByText('已阻塞')).not.toBeInTheDocument()
  })

  it('blocked + completed_pool_insufficient → 合格人数不足', () => {
    renderOverview(dashboardWith({ business_outcome: 'completed_pool_insufficient' }))
    expect(screen.getByText('本轮完成，合格人数不足')).toBeInTheDocument()
  })

  it('blocked + null → 流程阻塞，待处理（技术/流程语义，非业务未达标）', () => {
    renderOverview(dashboardWith({ business_outcome: null }))
    expect(screen.getByText('流程阻塞，待处理')).toBeInTheDocument()
  })

  it('failed → 技术失败', () => {
    renderOverview(dashboardWith({ status: 'failed', business_outcome: null }))
    expect(screen.getByText('技术失败')).toBeInTheDocument()
  })

  it('business_outcome 未知新值 → 回落 status 原逻辑（不渲染英文原形）', () => {
    renderOverview(dashboardWith({ business_outcome: 'completed_future_new' }))
    expect(screen.getByText('已阻塞')).toBeInTheDocument()
    expect(screen.queryByText('completed_future_new')).not.toBeInTheDocument()
  })
})
