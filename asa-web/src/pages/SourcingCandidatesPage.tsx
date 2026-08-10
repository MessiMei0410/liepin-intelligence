import { useEffect, useState } from 'react'
import { ArrowLeft, ExternalLink, LoaderCircle, UserRoundSearch, UsersRound, RefreshCw } from 'lucide-react'
import { api } from '../api'
import { WorkflowCandidateItem } from '../workflow/workflowSummary'
import { sourceLabel, date } from '../shared/format'

function resumeStatusLabel(status?: string) {
  if (!status || status === 'not_requested') return '未抓取'
  if (status === 'complete') return '已抓取'
  if (status === 'partial') return '部分抓取'
  if (status === 'failed') return '抓取失败'
  if (status === 'risk_paused') return '风控暂停'
  if (status === 'skipped_encrypted') return '已加密'
  return status
}

function resumeStatusTone(status?: string) {
  if (!status || status === 'not_requested') return 'neutral'
  if (status === 'complete') return 'good'
  if (status === 'partial') return 'warn'
  return 'bad'
}

function recommendationLabel(value?: string) {
  if (value === 'not_recommended') return '不推荐'
  if (value === 'verify_first') return '待补证据'
  return '待复核'
}

function recommendationTone(value?: string) {
  if (value === 'not_recommended') return 'bad'
  if (value === 'verify_first') return 'warn'
  return 'neutral'
}

export function SourcingCandidatesPage({
  workflowId,
  onBack,
  onOpenCandidate,
}: {
  workflowId: string
  onBack?: () => void
  onOpenCandidate?: (id: number) => void
}) {
  const PAGE_SIZE = 50
  const [items, setItems] = useState<WorkflowCandidateItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = async (offset = 0) => {
    setLoading(true)
    setError('')
    try {
      const page = await api.workflowCandidates(workflowId, PAGE_SIZE, offset)
      setItems(offset ? [...items, ...page.items] : page.items)
      setTotal(page.total)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId])

  return (
    <div className="sourcing-candidates-page">
      <header className="sourcing-candidates-head">
        {onBack && (
          <button className="icon-btn" onClick={onBack} aria-label="返回">
            <ArrowLeft />
          </button>
        )}
        <div>
          <h1>
            <UsersRound /> 寻访候选人名单
          </h1>
          <p>工作流 {workflowId} · 共 {total} 人</p>
        </div>
        <div className="sourcing-candidates-actions">
          <button className="button" disabled={loading} onClick={() => void load(0)}>
            {loading ? <LoaderCircle className="spin" /> : <RefreshCw />}
            刷新
          </button>
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="sourcing-candidates-table-wrap">
        <table className="sourcing-candidates-table">
          <thead>
            <tr>
              <th>姓名</th>
              <th>公司 / 职位</th>
              <th>来源渠道</th>
              <th>履历状态</th>
              <th>更新时间</th>
              <th>所在阶段</th>
              <th>意向</th>
              <th>ASA 评估</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((candidate) => (
              <tr key={candidate.id}>
                <td>
                  <b>{candidate.name || '姓名待补充'}</b>
                </td>
                <td>
                  <div className="candidate-company">{candidate.company || '公司待补充'}</div>
                  <div className="candidate-title">{candidate.title || '职位待补充'}</div>
                </td>
                <td>{sourceLabel(candidate.attribution?.channel || '')}</td>
                <td>
                  <span className={`tag ${resumeStatusTone(candidate.resume_capture_status)}`}>
                    {resumeStatusLabel(candidate.resume_capture_status)}
                  </span>
                  {candidate.resume_captured_at && (
                    <small>{date(candidate.resume_captured_at)}</small>
                  )}
                </td>
                <td>{date(candidate.updated_at || '')}</td>
                <td>
                  <div>{candidate.stage || '-'}</div>
                  <small>{candidate.flow_bucket || '-'}</small>
                </td>
                <td className="candidate-intention">{candidate.intention || '-'}</td>
                <td>
                  <div className="candidate-score">
                    <b>{candidate.fit_score ?? '-'}</b>
                    <span>{candidate.fit_level || ''}</span>
                  </div>
                  <span className={`tag ${recommendationTone(candidate.recommendation)}`}>
                    {recommendationLabel(candidate.recommendation)}
                  </span>
                </td>
                <td>
                  <button
                    className="button"
                    disabled={!onOpenCandidate}
                    onClick={() => onOpenCandidate?.(candidate.id)}
                  >
                    <ExternalLink />
                    查看
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {items.length === 0 && !loading && (
        <div className="insight-empty">
          <UserRoundSearch />
          <span>当前工作流暂未返回候选人名单。</span>
        </div>
      )}

      {items.length < total && (
        <div className="sourcing-candidates-more">
          <button className="button primary" disabled={loading} onClick={() => void load(items.length)}>
            {loading ? <LoaderCircle className="spin" /> : '加载更多'}
            （剩余 {total - items.length} 人）
          </button>
        </div>
      )}
    </div>
  )
}
