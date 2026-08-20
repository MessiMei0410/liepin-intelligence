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
      candidates: [
        { id: 1, name: 'A', company: 'C1', title: 'T1', stage: 'S1 新增寻访/待复核' },
        { id: 2, name: 'B', company: 'C2', title: 'T2', stage: 'S1 新增寻访/待复核' },
      ],
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

const flushMergeWindow = async () => {
  // 变更先入缓冲，800ms 合并窗口到点后才一次性应用
  await act(async () => { vi.advanceTimersByTime(800) })
}

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

  it('轮询变更经合并窗口后应用到名单数据（原位更新不移组）', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ changes: [changePayload(1, true)] }),
    })
    const onUpdate = vi.fn((updater: (prev: CandidateListCardData) => CandidateListCardData) => updater)
    renderHook(() => useCandidateListUpdates(makeData(), onUpdate))
    await act(async () => {})
    // 窗口未到点：尚未应用
    expect(onUpdate).not.toHaveBeenCalled()
    await flushMergeWindow()
    expect(onUpdate).toHaveBeenCalledTimes(1)
    const updated = (onUpdate.mock.calls[0][0] as (prev: CandidateListCardData) => CandidateListCardData)(makeData())
    // 原位：留在 active 分组，阶段标签更新；stopped 分组不新增人
    expect(updated.groups?.find(g => g.key === 'active')?.candidates.map(c => c.id)).toEqual([1, 2])
    expect(updated.groups?.find(g => g.key === 'active')?.candidates[0]?.stage).toBe('H5 初筛不通过')
    expect(updated.groups?.find(g => g.key === 'stopped')?.candidates).toHaveLength(0)
    expect(updated.summary?.active).toBe(1)
    expect(updated.summary?.stopped).toBe(1)
  })

  it('同一合并窗口内的多条变更一批一次应用', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ changes: [changePayload(1, true), changePayload(2, false)] }),
    })
    const onUpdate = vi.fn((updater: (prev: CandidateListCardData) => CandidateListCardData) => updater)
    renderHook(() => useCandidateListUpdates(makeData(), onUpdate))
    await act(async () => {})
    await flushMergeWindow()
    expect(onUpdate).toHaveBeenCalledTimes(1)
    const updated = (onUpdate.mock.calls[0][0] as (prev: CandidateListCardData) => CandidateListCardData)(makeData())
    const active = updated.groups?.find(g => g.key === 'active')
    expect(active?.candidates[0]?.stage).toBe('H5 初筛不通过')
    expect(active?.candidates[1]?.stage).toBe('S2 复核通过/待联系')
  })

  it('data/onUpdate 引用变化（同一岗位）不重启轮询', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ changes: [] }) })
    const { rerender } = renderHook(
      ({ data, onUpdate }) => useCandidateListUpdates(data, onUpdate),
      { initialProps: { data: makeData(), onUpdate: vi.fn() } },
    )
    await act(async () => {})
    expect(fetchMock).toHaveBeenCalledTimes(1)
    // 应用更新后的新 data 对象 + 新 onUpdate 引用：不得触发立即重 poll
    rerender({ data: { ...makeData() }, onUpdate: vi.fn() })
    await act(async () => {})
    expect(fetchMock).toHaveBeenCalledTimes(1)
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
