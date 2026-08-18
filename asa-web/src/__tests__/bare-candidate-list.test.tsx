import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BareCandidateList } from '../agent/BareCandidateList'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

const mocks = vi.hoisted(() => ({ candidateListRefresh: vi.fn() }))

vi.mock('../api', () => ({
  api: { candidateListRefresh: mocks.candidateListRefresh },
}))

const strictCard: CandidateListCardData = {
  type: 'candidate_list',
  title: '士兰微｜电源专家（岗位 142）分级过滤名单',
  context: { type: 'job', id: 142 },
  filter_mode: 'grade_filter',
  summary: { total: 277, active: 17, stopped: 260 },
  groups: [{
    key: 'A-核心',
    label: 'A-核心',
    candidates: [{ id: 619, name: '田逸帆', company: '台达', title: '电源工程师' }],
  }],
}

describe('BareCandidateList', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.candidateListRefresh.mockReset()
    mocks.candidateListRefresh.mockResolvedValue({ ok: true, answer: '严格名单', card: strictCard })
    ;(window as unknown as { __DETACHED_LIST__?: CandidateListCardData }).__DETACHED_LIST__ = strictCard
  })

  afterEach(() => {
    delete (window as unknown as { __DETACHED_LIST__?: CandidateListCardData }).__DETACHED_LIST__
    vi.useRealTimers()
  })

  it('刷新独立严格名单时保留 grade_filter 模式', async () => {
    render(<BareCandidateList onOpenCandidate={() => {}} />)
    await act(async () => { vi.advanceTimersByTime(200) })

    fireEvent.click(screen.getByLabelText('刷新名单'))
    await act(async () => { await Promise.resolve() })

    expect(mocks.candidateListRefresh).toHaveBeenCalledWith(142, false, 'grade_filter')
  })
})
