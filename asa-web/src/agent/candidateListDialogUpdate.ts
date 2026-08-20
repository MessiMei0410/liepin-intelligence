import type { CandidateListCardData } from '../workflows/CandidateListCard'

export type CandidateChange = {
  id: number
  stage?: string
  isStopped?: boolean
}

// 停止口径与名单卡服务端分组（a_system_agent.copilot_intent._STOP_TOKENS）对齐：
// 阶段文本命中停止词即视为已停止——即使上报方漏标 isStopped，
// 也不得出现“H5 初筛不通过”标签却留在可推进分组的矛盾态。
const STOP_STAGE_TOKENS = ['初筛不通过', '停止', '淘汰', '关闭']

/** 阶段文本是否命中停止口径（含系统态编码 screen_rejected）。 */
export const isStoppedStage = (stage?: string): boolean => {
  const text = (stage || '').toLowerCase()
  return STOP_STAGE_TOKENS.some(token => text.includes(token)) || text.includes('screen_rejected')
}

/**
 * 停止态判定：显式 isStopped=true 或新阶段命中停止口径即停止；
 * 显式 false 或带了非停止阶段则视为活跃；两者都缺省时维持当前态
 * （stopped 分组或现有阶段文本推断）。
 */
const resolveStopped = (change: CandidateChange, groupKey: string, currentStage?: string): boolean => {
  if (change.isStopped === true || isStoppedStage(change.stage)) return true
  if (change.isStopped === false || change.stage !== undefined) return false
  return groupKey === 'stopped' || isStoppedStage(currentStage)
}

/**
 * 根据候选人变更**原位**更新名单弹窗数据：
 * - 不跨组移动：停止的人留在原组原位置，仅更新阶段标签（停止类阶段由
 *   candidateStageTone 呈现“已停止”配色），避免轮询/事件刷新时名单跳动；
 * - 子集卡（subset=true，分组是评审结论）因此天然只更新标签与计数，不重构分组；
 * - summary 的 active/stopped 计数随停止态变化同步调整；
 * - 无实际变化时返回原引用，避免无效重渲染。
 */
export function updateCandidateListDialogData(
  data: CandidateListCardData,
  change: CandidateChange,
): CandidateListCardData {
  const inputGroups = data.groups ?? []
  let groupIndex = -1
  let candidateIndex = -1
  for (let i = 0; i < inputGroups.length; i += 1) {
    const idx = inputGroups[i].candidates.findIndex(c => c.id === change.id)
    if (idx >= 0) {
      groupIndex = i
      candidateIndex = idx
      break
    }
  }
  if (candidateIndex < 0) return data

  const group = inputGroups[groupIndex]
  const candidate = group.candidates[candidateIndex]
  const prevStopped = group.key === 'stopped' || isStoppedStage(candidate.stage)
  const nextStopped = resolveStopped(change, group.key, candidate.stage)
  const nextStage = change.stage ?? candidate.stage

  // 阶段与停止态都没变：原样返回，保持引用稳定（轮询重复投递时不触发重渲染）。
  if (nextStage === candidate.stage && nextStopped === prevStopped) return data

  const groups = inputGroups.map((item, index) => {
    if (index !== groupIndex) return item
    const candidates = item.candidates.slice()
    candidates[candidateIndex] = { ...candidate, stage: nextStage }
    return { ...item, candidates }
  })

  let summary = data.summary
  if (summary && nextStopped !== prevStopped) {
    summary = {
      ...summary,
      active: Math.max(0, Number(summary.active ?? 0) + (nextStopped ? -1 : 1)),
      stopped: Math.max(0, Number(summary.stopped ?? 0) + (nextStopped ? 1 : -1)),
    }
  }

  return { ...data, groups, summary }
}
