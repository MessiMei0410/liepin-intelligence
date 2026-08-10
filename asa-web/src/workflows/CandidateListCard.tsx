import { Users } from 'lucide-react'

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

export function CandidateListCard({
  data,
  onOpenCandidate,
  onOpenJob,
  compact = false,
}: {
  data: CandidateListCardData
  onOpenCandidate: (jobCandidateId: number) => void
  onOpenJob?: (jobId: number) => void
  compact?: boolean
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
        <span className="candidate-list-icon"><Users size={16} /></span>
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
                    <em className="candidate-list-stage">{candidate.stage || '—'}</em>
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
