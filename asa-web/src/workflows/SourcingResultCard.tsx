import { useMemo } from 'react'
import { Archive, CheckCircle, ExternalLink, MessageSquareText, Search, Trophy, UserRoundSearch, X, AlertTriangle } from 'lucide-react'

export type SourcingResultCandidate = {
  job_candidate_id: number
  name: string
  current_company?: string
  current_title?: string
  fit_score?: number
  fit_level?: string
  recommendation?: 'recommended' | 'verify_first' | 'not_recommended' | string
}

export type SourcingResultNextAction = {
  type: string
  label: string
}

export type SourcingResultSummary = {
  workflow_id: string
  round?: number | null
  client?: string
  job?: string
  status?: string
  business_outcome?: string | null
  assessed_count: number
  successful_count: number
  failed_count: number
  total_assessed_in_job: number
  recommendation_breakdown: {
    recommended: number
    verify_first: number
    not_recommended: number
  }
  top_candidates: SourcingResultCandidate[]
  next_actions: SourcingResultNextAction[]
}

export type SourcingResultCardData = {
  type: 'sourcing_result'
  title: string
  context?: { type: string; id: string | number }
  summary: SourcingResultSummary
}

const recommendationMeta: Record<string, { label: string; tone: string }> = {
  recommended: { label: '推荐', tone: 'green' },
  verify_first: { label: '待核验', tone: 'amber' },
  not_recommended: { label: '不推荐', tone: 'red' },
}

function recommendationLabel(value: string) {
  return recommendationMeta[value]?.label || value
}

function recommendationTone(value: string) {
  return recommendationMeta[value]?.tone || 'neutral'
}

function ScoreBadge({ score, level }: { score?: number; level?: string }) {
  if (score === undefined || score === null) return null
  const tone = score >= 75 ? 'green' : score >= 55 ? 'amber' : 'red'
  return (
    <span className={`sourcing-result-score ${tone}`}>
      {level ? `${level} · ` : ''}{score}
    </span>
  )
}

export function SourcingResultCard({
  data,
  onAction,
  onClose,
  onOpenCandidate,
  onOpenFullList,
  compact = false,
}: {
  data: SourcingResultCardData
  onAction?: (action: string, context?: { type: string; id: string | number }) => void
  onClose?: () => void
  onOpenCandidate?: (jobCandidateId: number) => void
  onOpenFullList?: () => void
  compact?: boolean
}) {
  const summary = data.summary
  const breakdown = summary.recommendation_breakdown || { recommended: 0, verify_first: 0, not_recommended: 0 }
  const total = Math.max(1, summary.successful_count)

  const actionButtons = useMemo(() => {
    const icons: Record<string, React.ReactNode> = {
      review_candidates: <UserRoundSearch size={14} />,
      discuss_strategy: <MessageSquareText size={14} />,
      archive: <Archive size={14} />,
      continue_sourcing: <Search size={14} />,
    }
    return (summary.next_actions || []).map(action => (
      <button
        key={action.type}
        className="button"
        onClick={() => onAction?.(action.type, data.context)}
      >
        {icons[action.type] || <CheckCircle size={14} />}
        {action.label}
      </button>
    ))
  }, [summary.next_actions, onAction, data.context])

  const isBlocked = summary.status === 'blocked' || (summary.business_outcome && summary.business_outcome !== 'completed_target_met')

  return (
    <div className={`sourcing-result-card ${compact ? 'compact' : ''}`} role="region" aria-label={data.title}>
      <div className="sourcing-result-head">
        <div>
          <h3>{data.title}</h3>
          <span className={`sourcing-result-status ${isBlocked ? 'amber' : 'green'}`}>
            {isBlocked ? <AlertTriangle size={14} /> : <Trophy size={14} />}
            {isBlocked ? '本轮完成，未达目标' : '本轮寻访已完成'}
          </span>
        </div>
        {onClose && (
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        )}
      </div>

      <div className="sourcing-result-stats">
        <div className="sourcing-result-stat">
          <b>{summary.assessed_count}</b>
          <span>本轮评估</span>
        </div>
        <div className="sourcing-result-stat">
          <b>{summary.successful_count}</b>
          <span>成功</span>
        </div>
        <div className="sourcing-result-stat">
          <b>{summary.failed_count}</b>
          <span>失败</span>
        </div>
        <div className="sourcing-result-stat">
          <b>{summary.total_assessed_in_job}</b>
          <span>岗位累计</span>
        </div>
      </div>

      <div className="sourcing-result-breakdown">
        {Object.entries(breakdown).map(([key, count]) => {
          const tone = recommendationTone(key)
          const pct = Math.round((Number(count) / total) * 100)
          return (
            <div key={key} className={`sourcing-result-breakdown-item ${tone}`}>
              <div className="breakdown-header">
                <span>{recommendationLabel(key)}</span>
                <strong>{count} ({pct}%)</strong>
              </div>
              <div className="breakdown-track">
                <i style={{ width: `${pct}%` }} />
              </div>
            </div>
          )
        })}
      </div>

      {!compact && summary.top_candidates && summary.top_candidates.length > 0 && (
        <div className="sourcing-result-candidates">
          <h4><Trophy size={14} /> Top 候选人</h4>
          <ul>
            {summary.top_candidates.map(candidate => (
              <button
                key={candidate.job_candidate_id}
                type="button"
                className="sourcing-result-candidate-row"
                disabled={!onOpenCandidate}
                onClick={() => onOpenCandidate?.(candidate.job_candidate_id)}
                aria-label={`打开人选：${candidate.name}`}
              >
                <div className="candidate-main">
                  <b>{candidate.name}</b>
                  <span>{[candidate.current_company, candidate.current_title].filter(Boolean).join(' · ')}</span>
                </div>
                <ScoreBadge score={candidate.fit_score} level={candidate.fit_level} />
                {candidate.recommendation && (
                  <span className={`candidate-tag ${recommendationTone(candidate.recommendation)}`}>
                    {recommendationLabel(candidate.recommendation)}
                  </span>
                )}
              </button>
            ))}
          </ul>
        </div>
      )}

      <div className="sourcing-result-actions" role="group" aria-label="下一步操作">
        {onOpenFullList && (
          <button className="button primary" onClick={onOpenFullList}>
            <ExternalLink size={14} />
            新标签页查看完整名单
          </button>
        )}
        {actionButtons}
      </div>
    </div>
  )
}
