import { useCallback, useEffect, useState } from 'react'
import { Check, ChevronDown, CircleDashed, CircleX, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '../api'
import type { SourcingAdjustment, SourcingAdjustmentEffect, SourcingAdjustmentListPayload, SourcingAdjustmentStatus, SourcingAdjustmentType } from '../api'
import { copilotText } from '../shared/text'
import { date } from '../shared/format'

// 停止备注 → 寻访调整：停止候选人时填写的备注经 LLM 分析成下一轮寻访调整指令。
// 待判断列表由顾问采纳/忽略；采纳后等待下一轮策略成功产出，不能提前冒充已应用。
// 6 类型中文标签与色调：补充/排除/过滤/薪资分别映射新增/负向/范围徽标。
const ADJUST_TYPE_LABELS: Record<SourcingAdjustmentType, string> = {
  add_keyword: '补充关键词',
  remove_keyword: '废弃关键词',
  exclude_company: '排除公司',
  add_company: '对标公司',
  add_filter: '过滤条件',
  adjust_salary_range: '薪资区间',
}
const ADJUST_TYPE_TONE: Record<SourcingAdjustmentType, string> = {
  add_keyword: 'ok',
  remove_keyword: 'warn',
  exclude_company: 'danger',
  add_company: 'ok',
  add_filter: 'warn',
  adjust_salary_range: 'muted',
}
const ADJUST_STATUS_TEXT: Record<SourcingAdjustmentStatus, string> = {
  pending: '待判断',
  accepted: '已采纳，待下轮策略',
  applied: '已应用',
  ignored: '已忽略',
}
const POOL_METRIC_LABELS: Array<{ key: keyof SourcingAdjustmentEffect['baseline']; label: string }> = [
  { key: 'total', label: '候选池' },
  { key: 'pending_review', label: '待复核' },
  { key: 'contacted', label: '已触达' },
  { key: 'stopped', label: '已停止' },
]

const summarize = (items: SourcingAdjustment[]) => ({
  pending: items.filter(item => item.status === 'pending').length,
  accepted: items.filter(item => item.status === 'accepted').length,
  applied: items.filter(item => item.status === 'applied').length,
  ignored: items.filter(item => item.status === 'ignored').length,
})

export function SourcingAdjustments({ jobId }: { jobId: number }) {
  const [data, setData] = useState<SourcingAdjustmentListPayload | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [actionError, setActionError] = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.sourcingAdjustments(jobId))
      setError('')
    } catch (reason) {
      setError(copilotText(reason) || '寻访调整读取失败')
    } finally {
      setLoading(false)
    }
  }, [jobId])
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load() }, [load])

  const act = async (id: number, kind: 'confirm' | 'ignore') => {
    setBusyId(id)
    setActionError('')
    try {
      const receipt = kind === 'confirm'
        ? await api.confirmSourcingAdjustment(id)
        : await api.ignoreSourcingAdjustment(id)
      setData(current => {
        if (!current || receipt.id !== id) return current
        const items = current.items.map(item => item.id === id ? receipt : item)
        return { ...current, items, summary: summarize(items) }
      })
    } catch (reason) {
      setActionError(copilotText(reason) || (kind === 'confirm' ? '采纳失败，请重试' : '忽略失败，请重试'))
    } finally {
      setBusyId(null)
    }
  }

  const items = data?.items ?? []
  const pending = items.filter(item => item.status === 'pending')
  const accepted = items.filter(item => item.status === 'accepted')
  const history = items.filter(item => item.status === 'applied' || item.status === 'ignored')
  const appliedCount = history.filter(item => item.status === 'applied').length
  const ignoredCount = history.filter(item => item.status === 'ignored').length

  return (
    <section className="job-detail-section sourcing-adjustments" aria-label="寻访调整">
      <h3>
        寻访调整{pending.length > 0 && <span className="job-section-count">{pending.length} 条待判断</span>}
      </h3>
      {actionError && <p className="sourcing-adjust-message error" role="alert"><CircleDashed />{actionError}</p>}
      {loading && !data && <div className="sourcing-adjust-message" role="status"><LoaderCircle className="spin" /><span>正在读取寻访调整…</span></div>}
      {error && <div className="sourcing-adjust-message error" role="alert"><CircleDashed /><span>{error}</span><button type="button" className="button" onClick={() => void load()}><RefreshCw />重试</button></div>}
      {!loading && !error && items.length === 0 && (
        <div className="empty">暂无寻访调整。停止候选人时填写备注，ASA 会分析成下一轮寻访调整。</div>
      )}
      {pending.length > 0 && (
        <div className="sourcing-adjust-list" aria-label="待判断调整列表">
          {pending.map(item => (
            <div className="sourcing-adjust-item" key={item.id}>
              <div className="sourcing-adjust-head">
                <span className={`tag ${ADJUST_TYPE_TONE[item.adjust_type]}`}>{ADJUST_TYPE_LABELS[item.adjust_type]}</span>
                <b title={item.value}>{item.value}</b>
              </div>
              <small className="sourcing-adjust-meta">来源 {item.candidate_name || '已删除候选人'}{item.created_at ? ` · ${date(item.created_at)}` : ''}</small>
              {item.rationale && <p className="sourcing-adjust-rationale" title={item.rationale}>{item.rationale}</p>}
              <div className="sourcing-adjust-actions">
                <button type="button" className="button confirm" disabled={busyId === item.id} onClick={() => void act(item.id, 'confirm')}>
                  {busyId === item.id ? <LoaderCircle className="spin" /> : <Check />}采纳调整
                </button>
                <button type="button" className="button ignore" disabled={busyId === item.id} onClick={() => void act(item.id, 'ignore')}>
                  <CircleX />忽略
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {accepted.length > 0 && (
        <div className="sourcing-adjust-history" role="region" aria-label="已采纳待应用调整">
          <strong className="sourcing-adjust-stage-title">已采纳，待下轮策略 <span>{accepted.length}</span></strong>
          <div className="sourcing-adjust-history-list">
            {accepted.map(item => (
              <div className="sourcing-adjust-history-item" key={item.id}>
                <div className="sourcing-adjust-head">
                  <span className={`tag ${ADJUST_TYPE_TONE[item.adjust_type]}`}>{ADJUST_TYPE_LABELS[item.adjust_type]}</span>
                  <b title={item.value}>{item.value}</b>
                </div>
                <small className="sourcing-adjust-meta">
                  {ADJUST_STATUS_TEXT.accepted}{item.accepted_at ? ` · ${date(item.accepted_at)}` : ''}
                </small>
              </div>
            ))}
          </div>
        </div>
      )}
      {history.length > 0 && (
        <div className="sourcing-adjust-history">
          <button type="button" className="button sourcing-adjust-toggle" aria-expanded={showHistory} onClick={() => setShowHistory(shown => !shown)}>
            <ChevronDown className={showHistory ? 'rotate' : ''} />已处理 {history.length} 条（已应用 {appliedCount} / 已忽略 {ignoredCount}）
          </button>
          {showHistory && (
            <div className="sourcing-adjust-history-list" aria-label="已处理调整列表">
              {history.map(item => (
                <div className="sourcing-adjust-history-item" key={item.id}>
                  <div className="sourcing-adjust-head">
                    <span className={`tag ${ADJUST_TYPE_TONE[item.adjust_type]}`}>{ADJUST_TYPE_LABELS[item.adjust_type]}</span>
                    <b title={item.value}>{item.value}</b>
                  </div>
                  <small className="sourcing-adjust-meta">
                    {item.status === 'applied'
                      ? (item.applied_round != null ? `已应用于第 ${item.applied_round} 轮寻访` : '已应用')
                      : ADJUST_STATUS_TEXT.ignored}
                    {item.applied_at ? ` · ${date(item.applied_at)}` : ''}
                    {item.applied_workflow_id ? ` · ${item.applied_workflow_id}` : ''}
                    {item.applied_artifact_id ? ` · 策略产物 ${item.applied_artifact_id}` : ''}
                  </small>
                  {item.status === 'applied' && item.effect && (
                    <div className="sourcing-adjust-effect" aria-label="调整前后候选池对比">
                      {POOL_METRIC_LABELS.map(metric => {
                        const diff = item.effect!.diff[metric.key]
                        if (diff === 0 && metric.key !== 'total') return null
                        const tone = diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat'
                        const label = `${item.effect!.baseline[metric.key]} → ${item.effect!.current[metric.key]}`
                        return (
                          <span key={metric.key} className={`effect-metric ${tone}`} title={label}>
                            {metric.label} {label}
                            {diff !== 0 && <em>{diff > 0 ? '+' : ''}{diff}</em>}
                          </span>
                        )
                      })}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
