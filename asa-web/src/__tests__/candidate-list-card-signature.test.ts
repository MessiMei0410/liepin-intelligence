import { describe, expect, it } from 'vitest'
import { candidateListCardSignature, isPendingConfirmRequest, listAutoOpenBlockedByConfirm, shouldAutoOpenCandidateList } from '../agent/candidateListCardSignature'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

const baseCard: CandidateListCardData = {
  type: 'candidate_list',
  title: '岗位 137 候选名单',
  context: { type: 'job', id: 137 },
  summary: { total: 2, active: 1, stopped: 1 },
  groups: [
    { key: 'active', label: '可推进', candidates: [{ id: 1203, name: '张雯' }] },
    { key: 'stopped', label: '已停止', candidates: [{ id: 1204, name: '李强' }] },
  ],
}

describe('名单卡签名（自动弹窗只对"新卡"触发）', () => {
  it('无历史卡时自动弹', () => {
    expect(shouldAutoOpenCandidateList(undefined, baseCard)).toBe(true)
  })

  it('同一张卡重复投影（DSH 卡片携带语义）不重复弹', () => {
    // DSH 委托轮会把上一轮名单卡并入后续 done，逐条回复重复到达（2026-08-19 dogfood
    // 弹窗反复遮挡正文）。内容逐项相同的卡签名一致 → 不弹。
    const replayed: CandidateListCardData = JSON.parse(JSON.stringify(baseCard))
    expect(candidateListCardSignature(replayed)).toBe(candidateListCardSignature(baseCard))
    expect(shouldAutoOpenCandidateList(baseCard, replayed)).toBe(false)
  })

  it('内容真实变化（刷新/新筛选）视为新卡，恢复自动弹', () => {
    const refreshed: CandidateListCardData = {
      ...baseCard,
      summary: { total: 3, active: 2, stopped: 1 },
      groups: [
        { key: 'active', label: '可推进', candidates: [{ id: 1203, name: '张雯' }, { id: 1205, name: '王芳' }] },
        { key: 'stopped', label: '已停止', candidates: [{ id: 1204, name: '李强' }] },
      ],
    }
    expect(shouldAutoOpenCandidateList(baseCard, refreshed)).toBe(true)
    // 不同岗位/不同口径也是新卡
    expect(shouldAutoOpenCandidateList(baseCard, { ...baseCard, context: { type: 'job', id: 138 } })).toBe(true)
    expect(shouldAutoOpenCandidateList(baseCard, { ...baseCard, filter_mode: 'grade_filter' })).toBe(true)
  })

  it('缺 summary/groups 的兜底卡签名稳定不抛错', () => {
    const sparse = { type: 'candidate_list', title: '子集名单' } as CandidateListCardData
    expect(candidateListCardSignature(sparse)).toBe(candidateListCardSignature({ ...sparse }))
    expect(shouldAutoOpenCandidateList(sparse, { ...sparse, title: '另一个子集' })).toBe(true)
  })
})

describe('确认卡优先于名单弹窗（dogfood R2-5）', () => {
  // 剧本 4 回归：合并预检确认卡出现时，前轮名单卡被携带重投又自动弹出，压住确认卡。
  const pendingConfirm = { kind: 'candidate_action', preflight_token: 'tok', action: 'merge' }
  it('isPendingConfirmRequest：无 state/未知 state 视为待确认，终态不算', () => {
    expect(isPendingConfirmRequest(pendingConfirm)).toBe(true)
    expect(isPendingConfirmRequest({ ...pendingConfirm, state: 'pending' })).toBe(true)
    expect(isPendingConfirmRequest({ ...pendingConfirm, state: 'confirmed' })).toBe(false)
    expect(isPendingConfirmRequest({ ...pendingConfirm, state: 'cancelled' })).toBe(false)
    expect(isPendingConfirmRequest(null)).toBe(false)
    expect(isPendingConfirmRequest(undefined)).toBe(false)
    expect(isPendingConfirmRequest('x')).toBe(false)
  })

  it('本轮 done 带待确认 confirm_request：名单弹窗不自动弹出（即使签名是"新卡"）', () => {
    expect(listAutoOpenBlockedByConfirm(pendingConfirm, [])).toBe(true)
    expect(listAutoOpenBlockedByConfirm(null, [])).toBe(false)
  })

  it('消息流已有活跃待确认卡：后续轮的名单卡同样不自动弹出', () => {
    const messages = [{ confirm_request: pendingConfirm }]
    expect(listAutoOpenBlockedByConfirm(null, messages)).toBe(true)
    // 确认卡已终态（confirmed/cancelled）后恢复自动弹
    expect(listAutoOpenBlockedByConfirm(null, [{ confirm_request: { ...pendingConfirm, state: 'confirmed' } }])).toBe(false)
    expect(listAutoOpenBlockedByConfirm(null, [{}, { confirm_request: null }])).toBe(false)
  })
})
