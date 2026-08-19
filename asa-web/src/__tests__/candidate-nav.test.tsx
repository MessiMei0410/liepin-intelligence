import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Candidates } from '../pages/Candidates'
import { CandidatePanel } from '../panels/CandidatePanel'
import type { Candidate } from '../api'
import { candidateDetail } from './helpers'

const makeCandidate = (id: number, extra: Partial<Candidate> = {}): Candidate => ({
  id,
  person_id: id,
  name: `候选人${id}`,
  client: 'ACME',
  job: '前端工程师',
  current_company: '示例科技',
  current_title: '高级工程师',
  source_type: 'liepin',
  clean_stage: 'S1 待复核',
  flow_bucket: '初筛',
  updated_at: '2026-08-05T10:00:00',
  ...extra,
})

describe('候选人详情页上一位/下一位导航', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('打开详情时携带当前筛选/排序后的完整顺序（含未翻到的页）', () => {
    const openCandidate = vi.fn()
    const items = [makeCandidate(1), makeCandidate(2), makeCandidate(3), makeCandidate(4, { clean_stage: '已停止' })]
    render(<Candidates items={items} openCandidate={openCandidate} />)
    // 默认"待处理"范围 + 相同更新时间按 id 倒序兜底：3 → 2 → 1（已停止的不在默认范围内）。
    fireEvent.click(screen.getByRole('row', { name: /候选人2/ }))
    expect(openCandidate).toHaveBeenCalledWith(2, [3, 2, 1])

    fireEvent.click(screen.getByRole('button', { name: /全部/ }))
    fireEvent.click(screen.getByRole('row', { name: /候选人4/ }))
    expect(openCandidate).toHaveBeenLastCalledWith(4, [4, 3, 2, 1])
  })

  it('compact 内嵌模式按调用方顺序传递', () => {
    const openCandidate = vi.fn()
    render(<Candidates items={[makeCandidate(1), makeCandidate(2)]} openCandidate={openCandidate} compact />)
    fireEvent.click(screen.getByRole('row', { name: /候选人1/ }))
    expect(openCandidate).toHaveBeenCalledWith(1, [1, 2])
  })

  it('详情页渲染导航按钮，点击按顺序切换候选人', () => {
    const onNavigate = vi.fn()
    render(
      <CandidatePanel
        value={candidateDetail}
        close={() => undefined}
        changed={() => undefined}
        nav={{ prevId: 5, nextId: 9, index: 1, total: 3 }}
        onNavigate={onNavigate}
      />,
    )
    expect(screen.getByRole('group', { name: '切换候选人' })).toHaveTextContent('2/3')

    fireEvent.click(screen.getByRole('button', { name: '上一位人选' }))
    expect(onNavigate).toHaveBeenCalledWith(5)

    fireEvent.click(screen.getByRole('button', { name: '下一位人选' }))
    expect(onNavigate).toHaveBeenCalledWith(9)
  })

  it('到达名单端点时对应按钮禁用', () => {
    render(
      <CandidatePanel
        value={candidateDetail}
        close={() => undefined}
        changed={() => undefined}
        nav={{ nextId: 2, index: 0, total: 2 }}
        onNavigate={() => undefined}
      />,
    )
    expect(screen.getByRole('button', { name: '上一位人选' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一位人选' })).toBeEnabled()
  })

  it('未提供导航信息时不渲染切换按钮', () => {
    render(<CandidatePanel value={candidateDetail} close={() => undefined} changed={() => undefined} />)
    expect(screen.queryByRole('group', { name: '切换候选人' })).not.toBeInTheDocument()
  })
})
