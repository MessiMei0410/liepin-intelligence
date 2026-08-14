import { sourceLabel } from '../shared/format'

export type SourcingAttribution = {
  source_type?: string
  channel?: string
  source_query?: string
  source_round?: string
  workflow_id?: string
  artifact_id?: string
  candidate_index?: number | null
  from_workflow?: boolean
}

export const sourcingAttributionStatus = (attribution?: SourcingAttribution | null) => (
  attribution?.from_workflow ? '本轮新增' : '历史入库'
)

export const sourcingAttributionChannel = (attribution?: SourcingAttribution | null) => (
  attribution?.source_type === 'mapping' ? 'Mapping 直挖' : sourceLabel(attribution?.channel || '')
)

export const sourcingAttributionQuery = (attribution?: SourcingAttribution | null) => {
  if (attribution?.source_type === 'mapping') {
    const artifact = attribution.artifact_id?.trim()
    const index = Number.isInteger(attribution.candidate_index) ? ` · 候选 ${Number(attribution.candidate_index) + 1}` : ''
    return artifact ? `任务卡：${artifact}${index}` : '任务卡来源已记录'
  }
  const query = attribution?.source_query?.trim()
  return query ? `关键词：${query}` : '关键词未记录'
}

export const sourcingAttributionRound = (attribution?: SourcingAttribution | null) => {
  const round = attribution?.source_round?.trim()
  return round ? ` · ${round}` : ''
}
