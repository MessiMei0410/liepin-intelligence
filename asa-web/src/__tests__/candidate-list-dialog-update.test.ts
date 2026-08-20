import { describe, expect, it } from 'vitest'
import { isStoppedStage, updateCandidateListDialogData } from '../agent/candidateListDialogUpdate'
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

describe('isStoppedStage（停止口径与名单卡 _STOP_TOKENS 对齐）', () => {
  it('命中停止词的阶段判为停止', () => {
    expect(isStoppedStage('H5 最近寻访/初筛不通过')).toBe(true)
    expect(isStoppedStage('H5 停止推进')).toBe(true)
    expect(isStoppedStage('H5 淘汰')).toBe(true)
    expect(isStoppedStage('H5 关闭')).toBe(true)
    expect(isStoppedStage('H5 screen_rejected')).toBe(true)
  })

  it('活跃阶段不误判', () => {
    expect(isStoppedStage('S1 新增寻访/待复核')).toBe(false)
    expect(isStoppedStage('S2 复核通过/待联系')).toBe(false)
    expect(isStoppedStage('H5 最近寻访')).toBe(false)
    expect(isStoppedStage(undefined)).toBe(false)
    expect(isStoppedStage('')).toBe(false)
  })
})

describe('updateCandidateListDialogData', () => {
  it('停止 active 分组候选人：原位更新不跨组移动，计数同步', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 2, stage: 'H5 最近寻访/初筛不通过', isStopped: true })
    const activeGroup = (next.groups ?? []).find(g => g.key === 'active')
    // 原位：仍留在 active 分组原下标（0），仅阶段标签更新
    expect(activeGroup?.candidates.map(c => c.id)).toEqual([2, 3])
    expect(activeGroup?.candidates[0]?.stage).toBe('H5 最近寻访/初筛不通过')
    // 不新建/追加 stopped 分组
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.map(c => c.id)).toEqual([4])
    expect(next.summary?.active).toBe(1)
    expect(next.summary?.stopped).toBe(2)
    expect(next.summary?.total).toBe(4)
  })

  it('停止 bonder 分组候选人：同样原位更新', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 1, stage: 'H5 最近寻访/初筛不通过', isStopped: true })
    expect((next.groups ?? []).find(g => g.key === 'bonder')?.candidates.map(c => c.id)).toEqual([1])
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.map(c => c.id)).toEqual([4])
    expect(next.summary?.active).toBe(1)
    expect(next.summary?.stopped).toBe(2)
  })

  it('口径对齐：isStopped 漏标为 false，但阶段命中停止词仍按停止处理', () => {
    const data = makeData()
    // 李** 矛盾态：H5 最近寻访/初筛不通过 标签 + isStopped=false（轮询上报缺漏）
    const next = updateCandidateListDialogData(data, { id: 2, stage: 'H5 最近寻访/初筛不通过', isStopped: false })
    expect(next.summary?.active).toBe(1)
    expect(next.summary?.stopped).toBe(2)
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates[0]?.stage).toBe('H5 最近寻访/初筛不通过')
  })

  it('复核通过：仅更新阶段，不改变分组与计数', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 3, stage: 'S2 复核通过/待联系', isStopped: false })
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates.find(c => c.id === 3)?.stage).toBe('S2 复核通过/待联系')
    expect(next.summary?.active).toBe(2)
    expect(next.summary?.stopped).toBe(1)
  })

  it('已停止人选恢复活跃：原位留在 stopped 分组，仅标签与计数更新', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 4, stage: 'S2 复核通过/待联系', isStopped: false })
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates.map(c => c.id)).toEqual([4])
    expect((next.groups ?? []).find(g => g.key === 'stopped')?.candidates[0]?.stage).toBe('S2 复核通过/待联系')
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates.map(c => c.id)).toEqual([2, 3])
    expect(next.summary?.active).toBe(3)
    expect(next.summary?.stopped).toBe(0)
  })

  it('子集卡（subset=true）：只更新标签与计数，不重构分组', () => {
    const data = makeData({
      subset: true,
      summary: { total: 2, active: 2, stopped: 0 },
      groups: [
        {
          key: 'review_passed',
          label: '建议复核',
          candidates: [
            { id: 7, name: '李某', stage: 'S1 新增寻访/待复核' },
            { id: 8, name: '王某', stage: 'S1 新增寻访/待复核' },
          ],
        },
      ],
    })
    const next = updateCandidateListDialogData(data, { id: 7, stage: 'H5 最近寻访/初筛不通过', isStopped: true })
    // 分组结构不变：不新建 stopped 分组，人不移组
    expect((next.groups ?? []).map(g => g.key)).toEqual(['review_passed'])
    expect((next.groups ?? [])[0]?.candidates.map(c => c.id)).toEqual([7, 8])
    expect((next.groups ?? [])[0]?.candidates[0]?.stage).toBe('H5 最近寻访/初筛不通过')
    expect(next.summary?.active).toBe(1)
    expect(next.summary?.stopped).toBe(1)
  })

  it('无实际变化：返回原引用（轮询重复投递不触发重渲染）', () => {
    const data = makeData()
    // 相同阶段 + 相同停止态
    expect(updateCandidateListDialogData(data, { id: 2, stage: 'S1 新增寻访/待复核', isStopped: false })).toBe(data)
    // 只带 id（无阶段无停止标记）
    expect(updateCandidateListDialogData(data, { id: 2 })).toBe(data)
  })

  it('候选人不在列表中：保持原数据', () => {
    const data = makeData()
    const next = updateCandidateListDialogData(data, { id: 999, stage: 'H5 初筛不通过', isStopped: true })
    expect(next).toBe(data)
  })

  it('没有 summary 时不崩溃', () => {
    const data = makeData({ summary: undefined })
    const next = updateCandidateListDialogData(data, { id: 2, stage: 'H5 初筛不通过', isStopped: true })
    expect((next.groups ?? []).find(g => g.key === 'active')?.candidates[0]?.stage).toBe('H5 初筛不通过')
    expect(next.summary).toBeUndefined()
  })
})
