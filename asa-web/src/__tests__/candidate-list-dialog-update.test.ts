import { describe, expect, it } from 'vitest'
import { updateCandidateListDialogData } from '../agent/candidateListDialogUpdate'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

const makeData = (overrides: Partial<CandidateListCardData> = {}): CandidateListCardData => ({
  type: 'candidate_list',
  title: '测试名单',
  context: { type: 'job', id: 1 },
  summary: { total: 4, active: 2, stopped: 1, bonder_count: 1 },
  groups: [
    {
      key: 'bonder',
      label: '固晶背景',
      priority: true,
      candidates: [{ id: 1, name: 'A', company: 'C1', title: 'T1', stage: 'S1 新增寻访/待复核' }],
    },
    {
      key: 'active',
      label: '其余可推进候选',
      candidates: [
        { id: 2, name: 'B', company: 'C2', title: 'T2', stage: 'S1 新增寻访/待复核' },
        { id: 3, name: 'C', company: 'C3', title: 'T3', stage: 'S2 复核通过/待联系' },
      ],
    },
    {
      key: 'stopped',
      label: '已停止推进',
      candidates: [{ id: 4, name: 'D', company: 'C4', title: 'T4', stage: 'H5 初筛不通过' }],
    },
  ],
  ...overrides,
})

describe('updateCandidateListDialogData', () => {
  it('停止 active 分组候选人：移入 stopped 分组并更新统计', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 2, stage: 'H5 最近寻访/初筛不通过', isStopped: true })
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates.some(c => c.id === 2)).toBe(false)
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.some(c => c.id === 2)).toBe(true)
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.find(c => c.id === 2)?.stage).toBe('H5 最近寻访/初筛不通过')
    expect(next.summary?.active).toBe(1)
    expect(next.summary?.stopped).toBe(2)
    expect(next.summary?.total).toBe(4)
  })

  it('停止 bonder 分组候选人：同样移入 stopped 分组', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 1, stage: 'H5 最近寻访/初筛不通过', isStopped: true })
    expect((next.groups ?? []).find(g => g.key === 'bonder')?.candidates.some(c => c.id === 1)).toBe(false)
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.some(c => c.id === 1)).toBe(true)
    expect(next.summary?.active).toBe(1)
    expect(next.summary?.stopped).toBe(2)
  })

  it('复核通过：仅更新阶段，不改变分组', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 3, stage: 'S2 复核通过/待联系', isStopped: false })
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates.find(c => c.id === 3)?.stage).toBe('S2 复核通过/待联系')
    expect(next.summary?.active).toBe(2)
    expect(next.summary?.stopped).toBe(1)
  })

  it('非停止操作把 stopped 分组候选人移回 active', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 4, stage: 'S2 复核通过/待联系', isStopped: false })
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.some(c => c.id === 4)).toBe(false)
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates.some(c => c.id === 4)).toBe(true)
    expect(next.summary?.active).toBe(3)
    expect(next.summary?.stopped).toBe(0)
  })

  it('候选人不在列表中：保持原数据', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 999, stage: 'H5 初筛不通过', isStopped: true })
    expect(next).toEqual(data)
  })

  it('没有 summary 时不崩溃', () => {
    const data = makeData({ summary: undefined })
    const next = updateCandidateListDialogData(data, { id: 2, stage: 'H5 初筛不通过', isStopped: true })
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.some(c => c.id === 2)).toBe(true)
    expect(next.summary).toBeUndefined()
  })
})
