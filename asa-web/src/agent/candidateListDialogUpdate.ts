import type { CandidateListCardData, CandidateListCandidate, CandidateListGroup } from '../workflows/CandidateListCard'

export type CandidateChange = {
  id: number
  stage?: string
  isStopped?: boolean
}

/** 根据候选人变更更新名单弹窗数据：停止则移入 stopped 分组，否则更新阶段。 */
export function updateCandidateListDialogData(
  data: CandidateListCardData,
  change: CandidateChange,
): CandidateListCardData {
  const inputGroups = data.groups ?? []
  const groups = inputGroups.map(group => ({ ...group, candidates: group.candidates.map(c => ({ ...c })) }))
  let targetIndex = -1
  let targetGroupKey = ''
  for (let i = 0; i < groups.length; i += 1) {
    const idx = groups[i].candidates.findIndex(c => c.id === change.id)
    if (idx >= 0) {
      targetIndex = idx
      targetGroupKey = groups[i].key
      break
    }
  }
  if (targetIndex < 0) return data

  const summary = data.summary ? { ...data.summary } : undefined

  if (change.isStopped) {
    // 已停止：从非 stopped 分组移除，追加到 stopped 分组末尾
    if (targetGroupKey !== 'stopped') {
      const candidate = groups.find(g => g.key === targetGroupKey)?.candidates.splice(targetIndex, 1)[0]
      if (candidate) {
        if (change.stage) candidate.stage = change.stage
        let stoppedGroup = groups.find(g => g.key === 'stopped')
        if (!stoppedGroup) {
          stoppedGroup = { key: 'stopped', label: '已停止推进', candidates: [] }
          groups.push(stoppedGroup)
        }
        stoppedGroup.candidates.push(candidate)
        if (summary) {
          summary.active = Math.max(0, (summary.active ?? 0) - 1)
          summary.stopped = (summary.stopped ?? 0) + 1
        }
      }
    } else if (change.stage) {
      const candidate = groups.find(g => g.key === targetGroupKey)?.candidates[targetIndex]
      if (candidate) candidate.stage = change.stage
    }
  } else {
    // 非停止：仅更新阶段；若当前在 stopped 分组则移回 active 分组
    const candidate = groups.find(g => g.key === targetGroupKey)?.candidates[targetIndex]
    if (candidate && change.stage) candidate.stage = change.stage
    if (targetGroupKey === 'stopped') {
      const moved = groups.find(g => g.key === targetGroupKey)?.candidates.splice(targetIndex, 1)[0]
      if (moved) {
        let activeGroup = groups.find(g => g.key === 'active')
        if (!activeGroup) {
          activeGroup = { key: 'active', label: '其余可推进候选', candidates: [] }
          groups.push(activeGroup)
        }
        activeGroup.candidates.push(moved)
        if (summary) {
          summary.active = (summary.active ?? 0) + 1
          summary.stopped = Math.max(0, (summary.stopped ?? 0) - 1)
        }
      }
    }
  }

  return { ...data, groups, summary }
}
