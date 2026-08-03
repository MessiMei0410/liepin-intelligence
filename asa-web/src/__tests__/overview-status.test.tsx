import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Overview } from '../pages/Overview'
import type { Dashboard } from '../api'
import { tabs } from '../shared/tabs'

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
  it('顶级导航只保留四个工作区，Agent 替代总览', () => {
    expect(tabs.map(([, label]) => label)).toEqual(['Agent', '岗位看板', '人选进度', '人选列表'])
    renderOverview(dashboardWith({}))

    const radar = screen.getByRole('button', { name: '人才雷达' })
    expect(radar).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(radar)
    expect(radar).toHaveAttribute('aria-expanded', 'true')
  })

  it('行动卡入口同时展示待确认、已执行和失败数量', () => {
    renderOverview({ ...dashboardWith({}), counts: { active_jobs: 1, candidates: 1, pending_candidates: 1, pending_approvals: 0, pending_proposals: 2, executed_proposals: 5, failed_proposals: 1 } })
    expect(screen.getByText('Agent 行动卡')).toBeInTheDocument()
    expect(screen.getByText('待确认 2 · 已执行 5 · 失败 1')).toBeInTheDocument()
  })

  it('只有历史已执行记录时不占用总览主工作区', () => {
    renderOverview({ ...dashboardWith({}), counts: { active_jobs: 1, candidates: 1, pending_candidates: 1, pending_approvals: 0, pending_proposals: 0, executed_proposals: 50, failed_proposals: 0 } })
    expect(screen.queryByText('Agent 行动卡')).not.toBeInTheDocument()
  })

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

  it('superseded 与未知工作流状态都不渲染英文原形', () => {
    const { rerender } = renderOverview(dashboardWith({ status: 'superseded' }))
    expect(screen.getByText('已被修订版替代')).toBeInTheDocument()
    expect(screen.queryByText('superseded')).not.toBeInTheDocument()

    rerender(<Overview dashboard={dashboardWith({ status: 'future_status' })} jobs={[]} candidates={[]} openWorkflow={() => {}} openCandidate={() => {}} archiveWorkflow={() => {}} />)
    expect(screen.getByText('状态待同步')).toBeInTheDocument()
    expect(screen.queryByText('future_status')).not.toBeInTheDocument()
  })
})
