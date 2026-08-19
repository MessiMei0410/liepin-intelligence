import { LoaderCircle, RefreshCw, Users } from 'lucide-react'

export type CandidateListCandidate = {
  id: number
  name: string
  company?: string
  title?: string
  stage?: string
  flow_bucket?: string
}

export type CandidateListGroup = {
  key: string
  label: string
  priority?: boolean
  candidates: CandidateListCandidate[]
}

export type CandidateListCardData = {
  type: 'candidate_list'
  title: string
  context?: { type: string; id: string | number }
  filter_mode?: 'grade_filter'
  /** 子集名单卡（精读/评审/去重等指定一组候选人，POST /api/v1/candidates/list-card）：
   *  刷新语义只对整池卡成立，前端据此隐藏「刷新」按钮。 */
  subset?: boolean
  summary?: {
    total?: number
    active?: number
    stopped?: number
    bonder_count?: number
  }
  groups?: CandidateListGroup[]
}

const GROUP_LIMIT: Record<string, number> = {
  bonder: 12,
  active: 15,
  stopped: 5,
}

/** 阶段标签配色：按 clean_stage 关键词归类到 5 档色板，返回 CSS class 后缀。 */
export function candidateStageTone(stage?: string): string {
  const s = stage || ''
  if (/(初筛不通过|停止|淘汰|关闭|拒绝|不推进)/.test(s)) return 'stopped'
  if (/(复核通过|待联系|已推荐|推荐)/.test(s)) return 'passed'
  if (/(已联系|加微信|已申请)/.test(s)) return 'contacted'
  if (/(已触达|触达)/.test(s)) return 'reached'
  if (/(待复核|新增寻访|待筛|最近寻访|X-SaaS)/.test(s)) return 'review'
  return 'default'
}

export function CandidateListCard({
  data,
  onOpenCandidate,
  onOpenJob,
  compact = false,
  onRefresh,
  refreshing = false,
}: {
  data: CandidateListCardData
  onOpenCandidate: (jobCandidateId: number) => void
  onOpenJob?: (jobId: number) => void
  compact?: boolean
  onRefresh?: () => void
  refreshing?: boolean
}) {
  const summary = data.summary || {}
  const groups = Array.isArray(data.groups) ? data.groups : []
  const total = Number(summary.total ?? groups.reduce((sum, group) => sum + (group.candidates?.length || 0), 0))
  const active = Number(summary.active ?? total)
  const stopped = Number(summary.stopped ?? 0)
  const bonderCount = Number(summary.bonder_count ?? groups.find(group => group.key === 'bonder')?.candidates?.length ?? 0)
  const jobId = data.context?.type === 'job' ? Number(data.context.id) : undefined

  return (
    <div className={`candidate-list-card ${compact ? 'compact' : ''}`} role="region" aria-label={data.title}>
      <div className="candidate-list-head">
        <div>
          <h3>{data.title}</h3>
          <span className="candidate-list-meta">
            共 {total} 人{bonderCount > 0 ? ` · 固晶/共晶/键合背景 ${bonderCount} 人` : ''} · 可推进 {active} 人 · 已停止 {stopped} 人
          </span>
        </div>
        <span className="candidate-list-head-actions">
          {onRefresh && <button className="candidate-list-refresh" onClick={onRefresh} disabled={refreshing} title="重新按库内最新状态生成名单">{refreshing ? <LoaderCircle className="spin" size={14}/> : <RefreshCw size={14}/>}<span>{refreshing ? '刷新中' : '刷新'}</span></button>}
          <span className="candidate-list-icon"><Users size={16} /></span>
        </span>
      </div>
      {groups.map(group => {
        const candidates = group.candidates || []
        if (!candidates.length) return null
        const limit = GROUP_LIMIT[group.key] ?? 10
        const visible = candidates.slice(0, limit)
        const hiddenCount = Math.max(0, candidates.length - visible.length)
        return (
          <div className={`candidate-list-group ${group.priority ? 'priority' : ''}`} key={group.key}>
            <h4>{group.priority ? '⭐ ' : ''}{group.label}<em>{candidates.length} 人</em></h4>
            <ul>
              {visible.map(candidate => (
                <li key={candidate.id}>
                  <button
                    className="candidate-list-row"
                    onClick={() => onOpenCandidate(candidate.id)}
                    title={`打开人选详情：${candidate.name}`}
                  >
                    <span className="candidate-list-row-main">
                      <b>{candidate.name}</b>
                      <small>{[candidate.company, candidate.title].filter(Boolean).join(' · ')}</small>
                    </span>
                    <em className={`candidate-list-stage tone-${candidateStageTone(candidate.stage)}`}>{candidate.stage || '—'}</em>
                  </button>
                </li>
              ))}
            </ul>
            {hiddenCount > 0 && <p className="candidate-list-more">其余 {hiddenCount} 人未展开，可在岗位页查看完整名单</p>}
          </div>
        )
      })}
      {jobId !== undefined && onOpenJob && (
        <div className="candidate-list-foot">
          <button className="button" onClick={() => onOpenJob(jobId)}>打开岗位查看完整名单</button>
        </div>
      )}
    </div>
  )
}
