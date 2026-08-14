import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SourcingResultCard, type SourcingResultCardData } from '../workflows/SourcingResultCard'

const mockCard: SourcingResultCardData = {
  type: 'sourcing_result',
  title: '寻访结果：士兰微 · 电源专家 · 第3轮',
  context: { type: 'workflow', id: 'workflow_8a57e861a20d' },
  summary: {
    workflow_id: 'workflow_8a57e861a20d',
    round: 3,
    client: '士兰微',
    job: '电源专家',
    status: 'completed',
    business_outcome: null,
    assessed_count: 50,
    successful_count: 47,
    failed_count: 3,
    total_assessed_in_job: 138,
    recommendation_breakdown: { recommended: 12, verify_first: 8, not_recommended: 27 },
    top_candidates: [
      { job_candidate_id: 835, name: '王**', current_company: 'A公司', current_title: '高级工程师', fit_score: 92, fit_level: 'A+', recommendation: 'recommended' },
      { job_candidate_id: 836, name: '李**', current_company: 'B公司', current_title: '技术专家', fit_score: 88, fit_level: 'A', recommendation: 'verify_first' },
    ],
    next_actions: [
      { type: 'review_candidates', label: '复核现有人选' },
      { type: 'discuss_strategy', label: '调整寻访策略' },
      { type: 'archive', label: '结束本轮' },
    ],
  },
}

describe('SourcingResultCard', () => {
  it('渲染标题与核心统计', () => {
    render(<SourcingResultCard data={mockCard} />)
    expect(screen.getByText('寻访结果：士兰微 · 电源专家 · 第3轮')).toBeInTheDocument()
    expect(screen.getByText('50')).toBeInTheDocument()
    expect(screen.getByText('47')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('138')).toBeInTheDocument()
  })

  it('渲染推荐等级分布与 Top 候选人', () => {
    render(<SourcingResultCard data={mockCard} />)
    expect(screen.getAllByText('推荐').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('待补证据').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('不推荐')).toBeInTheDocument()
    expect(screen.getByText('王**')).toBeInTheDocument()
    expect(screen.getByText('李**')).toBeInTheDocument()
  })

  it('渲染下一步操作按钮', () => {
    const onAction = vi.fn()
    render(<SourcingResultCard data={mockCard} onAction={onAction} />)
    expect(screen.getByRole('button', { name: '复核现有人选' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '调整寻访策略' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '结束本轮' })).toBeInTheDocument()
  })

  it('点击操作按钮触发 onAction', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    render(<SourcingResultCard data={mockCard} onAction={onAction} />)
    await user.click(screen.getByRole('button', { name: '复核现有人选' }))
    expect(onAction).toHaveBeenCalledWith('review_candidates', { type: 'workflow', id: 'workflow_8a57e861a20d' })
  })

  it('点击关闭触发 onClose', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<SourcingResultCard data={mockCard} onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: '关闭' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('compact 模式不渲染 Top 候选人', () => {
    render(<SourcingResultCard data={mockCard} compact />)
    expect(screen.queryByText('Top 候选人')).not.toBeInTheDocument()
  })
})
