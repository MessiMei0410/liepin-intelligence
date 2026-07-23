import type { MappingCandidateStatus, MappingFailure, MappingTargetTeam, MappingTaskStats } from '../api'

// S5-2 Mapping 任务卡的纯展示逻辑（新文件，供 MappingTaskCard 与入口按钮共用，单测直打）。
// 文案一律 UX-1 业务语言：confidence→把握大/一般/偏小，stats→这份名单的效果，
// 失败记账说人话（如"官网有反爬保护，没抓到"）。

// 七态中文标签（与后端 CANDIDATE_STATUS_LABELS 同文；前端自持一份避免状态未到先空白）。
export const MAPPING_STATUS_LABELS: Record<MappingCandidateStatus, string> = {
  pending: '待确认',
  confirmed: '已确认',
  contacted: '已接触',
  replied: '已回复',
  intaken: '已入库',
  parked: '已搁置',
  rejected: '已淘汰',
}

export const mappingStatusLabel = (status: string): string =>
  MAPPING_STATUS_LABELS[status as MappingCandidateStatus] || status || '待确认'

// 置信度→把握：high 把握大 / medium 一般 / low 偏小；未知值如实显示原文。
export const mappingConfidenceLabel = (confidence?: string): string => {
  if (confidence === 'high') return '把握大'
  if (confidence === 'medium') return '把握一般'
  if (confidence === 'low') return '把握偏小'
  return confidence || '把握待评估'
}

// 决策树 escalate_mapping 步触发 Mapping 直挖时上报后端的 trigger 枚举（后端只认
// decision_tree_exhausted / manual，别处硬编码会 409）。
export const MAPPING_TRIGGER_BY_TREE = 'decision_tree_exhausted'

export const MAPPING_ARTIFACT_TYPE = 'mapping_task'

// 工作流产物列表里找任务卡 artifact_id（已存在则直接打开，不重复发起采集）。
export const findMappingArtifactId = (artifacts: Array<{ artifact_id: string; artifact_type: string }>): string =>
  artifacts.find(item => item.artifact_type === MAPPING_ARTIFACT_TYPE)?.artifact_id || ''

// 目标团队按公司分组（同一公司可有多个团队），保持原始顺序。
export type MappingTeamGroup = { company: string; teams: MappingTargetTeam[] }
export const groupMappingTeams = (teams: MappingTargetTeam[]): MappingTeamGroup[] => {
  const groups: MappingTeamGroup[] = []
  const byCompany = new Map<string, MappingTargetTeam[]>()
  for (const team of teams) {
    const company = team.company || '公司待确认'
    const list = byCompany.get(company)
    if (list) list.push(team)
    else {
      const created: MappingTargetTeam[] = [team]
      byCompany.set(company, created)
      groups.push({ company, teams: created })
    }
  }
  return groups
}

// 失败记账人话映射：reason 是机器分类，note 是后端补充说明；展示先说人话，note 作补充。
// 未知 reason 不硬编，原文透出（附 note），不静默。
export const humanizeMappingFailure = (failure: MappingFailure): string => {
  const source = failure.source || '来源'
  const reason = failure.reason || ''
  if (reason === 'blocked') return `${source}有反爬保护，没抓到`
  if (reason === 'timeout') return `${source}访问超时，没抓到`
  if (reason === 'no_site_hint') return `没有找到${source}入口，已跳过`
  if (reason === 'parse_error') return `${source}页面结构变了，没解析出来`
  if (reason === 'skipped_after_failure') return failure.note || `${source}连不上，后续请求已跳过`
  const http = /^http_(\d+)$/.exec(reason)
  if (http) return `${source}返回异常（HTTP ${http[1]}），没抓到`
  return failure.note ? `${source}没抓到：${reason}（${failure.note}）` : `${source}没抓到：${reason || '原因未知'}`
}

// 「这份名单的效果」摘要数：线索有效率 = 已确认（含推进到之后各态）/ 采集线索数。
// clues 为 0 时不算比例（不硬编 0%）。
export const mappingClueRate = (stats: MappingTaskStats): { confirmed: number; clues: number; percent: number | null } => {
  const confirmed = stats.confirmed ?? 0
  const clues = stats.clues ?? 0
  return { confirmed, clues, percent: clues > 0 ? Math.round((confirmed / clues) * 100) : null }
}
