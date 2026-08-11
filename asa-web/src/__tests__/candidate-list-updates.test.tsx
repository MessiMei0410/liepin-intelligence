import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useCandidateListUpdates } from '../agent/useCandidateListUpdates'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

const makeData = (): CandidateListCardData => ({
  type: 'candidate_list',
  title: '测试名单',
  context: { type: 'job', id: 137 },
  summary: { total: 2, active: 2, stopped: 0 },
  groups: [
    {
      key: 'active',
      label: '其余可推进候选',
      candidates: [{ id: 1, name: 'A', company: 'C1', title: 'T1', stage: 'S1 新增寻访/待复核' }],
    },
    { key: 'stopped', label: '已停止推进', candidates: [] },
  ],
})

const changePayload = (jobCandidateId: number, isStopped: boolean) => ({
  job_candidate_id: jobCandidateId,
  is_stopped: isStopped,
  stage: isStopped ? 'H5 初筛不通过' : 'S2 复核通过/待联系',
  updated_at: new Date().toISOString(),
})

describe('useCandidateListUpdates', () => {
  const fetchMock = vi.fn()
  beforeEach(() => {
    vi.useFakeTimers()
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('首次轮询就把服务端变更应用到名单数据', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ changes: [changePayload(1, true)] }),
    })
    const onUpdate = vi.fn((updater: (prev: CandidateListCardData) => CandidateListCardData) => updater)
    renderHook(() => useCandidateListUpdates(makeData(), onUpdate))
    await act(async () => {})
    expect(onUpdate).toHaveBeenCalledTimes(1)
    const updated = (onUpdate.mock.calls[0][0] as (prev: CandidateListCardData) => CandidateListCardData)(makeData())
    expect(updated.groups?.find(g => g.key === 'stopped')?.candidates.some(c => c.id === 1)).toBe(true)
  })

  it('interval 轮询携带 since 参数', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ changes: [changePayload(1, true)] }) })
    renderHook(() => useCandidateListUpdates(makeData(), vi.fn()))
    await act(async () => {})
    fetchMock.mockClear()
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ changes: [] }) })
    await act(async () => { vi.advanceTimersByTime(2500) })
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/asa/floating/candidate-updates?job_id=137')
    expect(url).toContain('since=')
  })

  it('job_id 不存在时不轮询', async () => {
    const data = { ...makeData(), context: undefined }
    renderHook(() => useCandidateListUpdates(data as CandidateListCardData, vi.fn()))
    await act(async () => {})
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
