import { useCallback, useEffect, useRef, useState } from 'react'
import { CircleCheck, LoaderCircle, MessageSquarePlus, PackageOpen, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type { RecommendationPackageDetailPayload, RecommendationPackageFeedback, RecommendationPackageSummary } from '../api'
import { date } from '../shared/format'
import { copilotText } from '../shared/text'
import { SectionHead } from '../shared/primitives'
import { recommendationPackageFreshness } from './recommendationPackageFreshness'

// 版本化推荐包（推荐闭环）：顾问确认推荐后由 Core 聚合生成（候选摘要/人岗证据/风险/待核验问题），
// 客户反馈按包版本记录并回写候选人事件时间线。
// 本组件负责：
//  1) 版本列表（数据随候选人详情 recommendation_packages 下发，无确认推荐不渲染）；
//  2) 展开查看包内容（按需拉详情接口）；
//  3) 记录客户反馈（反馈类型枚举 + 内容，走 write 幂等封装；成功后回执 + 回读详情刷新）。
// 红线：如实反映后端状态（失败不当成功）、不引入 any、不用 prompt/confirm/alert、样式只走 reco-package- 前缀。

const feedbackTypeOptions: Array<{ value: string; label: string }> = [
  { value: 'approved', label: '客户认可' },
  { value: 'interview', label: '安排面试' },
  { value: 'rejected', label: '客户否决' },
  { value: 'hold', label: '暂缓推进' },
  { value: 'other', label: '其他反馈' },
]

const mergeFeedback = (current: RecommendationPackageFeedback[] = [], incoming: RecommendationPackageFeedback[] = []) =>
  [...new Map([...current, ...incoming].map(item => [item.id, item])).values()]

export function RecommendationPackagesSection({ packages, onFeedbackRecorded }: {
  packages: RecommendationPackageSummary[]
  onFeedbackRecorded?: () => void | Promise<void>
}) {
  const [openId, setOpenId] = useState('')
  if (!packages.length) return null
  return (
    <>
      <SectionHead title="推荐包" meta={`${packages.length} 个版本`} />
      {packages.map((item) => (
        <div className="reco-package" key={item.package_id}>
          <button
            type="button"
            className="aside-item artifact-item"
            onClick={() => setOpenId(openId === item.package_id ? '' : item.package_id)}
            aria-expanded={openId === item.package_id}
            aria-label={`查看推荐包 v${item.version}`}
          >
            <PackageOpen />
            <span>
              <b>推荐包 v{item.version}</b>
              <small>{date(item.created_at)} · 客户反馈 {item.feedback_count ?? 0} 条</small>
            </span>
          </button>
          {openId === item.package_id && <RecommendationPackageDetail packageId={item.package_id} onFeedbackRecorded={onFeedbackRecorded} />}
        </div>
      ))}
    </>
  )
}

function RecommendationPackageDetail({ packageId, onFeedbackRecorded }: {
  packageId: string
  onFeedbackRecorded?: () => void | Promise<void>
}) {
  const [detail, setDetail] = useState<RecommendationPackageDetailPayload>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [feedbackType, setFeedbackType] = useState('approved')
  const [feedbackContent, setFeedbackContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [receipt, setReceipt] = useState<{ tone: 'error' | 'success'; text: string }>()
  const refreshSeq = useRef(0)

  // 展开时按需拉取一次（挂载即拉）；手动重试调用 load。
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setDetail(await api.recommendationPackage(packageId))
    } catch (e) {
      setError(copilotText(e) || '推荐包详情加载失败，请重试。')
    } finally {
      setLoading(false)
    }
  }, [packageId])
  useEffect(() => {
    let alive = true
    api.recommendationPackage(packageId)
      .then((payload) => { if (alive) { setDetail(payload); setLoading(false) } })
      .catch((e) => { if (alive) { setError(copilotText(e) || '推荐包详情加载失败，请重试。'); setLoading(false) } })
    return () => { alive = false }
  }, [packageId])

  const submitFeedback = async () => {
    if (busy) return
    if (!feedbackContent.trim()) {
      setReceipt({ tone: 'error', text: '请先填写客户反馈内容，再提交。' })
      return
    }
    setBusy(true)
    setReceipt(undefined)
    try {
      const result = await api.recordPackageFeedback(packageId, { feedback_type: feedbackType, content: feedbackContent.trim() })
      const label = result.feedback?.feedback_type_label || feedbackTypeOptions.find((o) => o.value === feedbackType)?.label || feedbackType
      if (result.feedback) {
        const recordedFeedback = result.feedback
        setDetail(current => current ? {
          ...current,
          feedback: mergeFeedback(current.feedback, [recordedFeedback]),
        } : current)
      }
      setReceipt({
        tone: 'success',
        text: result.already_recorded || result.receipt?.idempotent_replay
          ? `该反馈此前已记录（${label}），已同步最新状态。`
          : `客户反馈已记录（${label}），并写入候选人时间线。`,
      })
      setFeedbackContent('')
      const refreshId = ++refreshSeq.current
      void api.recommendationPackage(packageId).then(payload => {
        if (refreshId !== refreshSeq.current) return
        setDetail(current => ({ ...payload, feedback: mergeFeedback(payload.feedback, current?.feedback) }))
      }).catch(() => undefined)
      void Promise.resolve().then(() => onFeedbackRecorded?.()).catch(() => undefined)
    } catch (e) {
      setReceipt({ tone: 'error', text: `客户反馈记录失败：${copilotText(e) || '请稍后重试'}。可重试，重复提交不会重复记录。` })
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div className="reco-package-loading"><LoaderCircle className="spin" /><span>推荐包加载中…</span></div>
  if (error) return <div className="reco-package-error"><TriangleAlert /><span>{error}</span><button type="button" className="button" onClick={() => { void load() }}>重试</button></div>
  if (!detail) return null
  const summary = detail.summary || {}
  const evidence = detail.evidence || {}
  const risks = detail.risks || []
  const questions = detail.verification_questions || []
  const feedback = detail.feedback || []
  const freshness = recommendationPackageFreshness(detail.created_at, evidence.assessed_at)
  return (
    <div className="reco-package-detail">
      {freshness.stale && <div className="reco-package-stale" role="status">
        <TriangleAlert />
        <span>当前人岗评估在推荐包生成后已更新；本包证据仅供对照，推荐包升版需由后端版本化接口生成。</span>
      </div>}
      <section>
        <h4>候选摘要</h4>
        <p>{[summary.current_title, summary.current_company].filter(Boolean).join(' @ ') || summary.name || '-'}</p>
        <small>{[summary.city, summary.education, summary.experience].filter(Boolean).join(' · ') || '基础信息待补充'}</small>
        {summary.job && <small>目标岗位：{[summary.job.client, summary.job.title].filter(Boolean).join(' · ') || '-'}</small>}
        {summary.recommendation?.reason && <small>推荐理由：{summary.recommendation.reason}（{date(summary.recommendation.confirmed_at)}）</small>}
      </section>
      <section>
        <h4>人岗匹配证据</h4>
        {evidence.status === 'ready' ? (
          <>
            <p>匹配度 {evidence.fit_score ?? '-'}（{evidence.fit_level || '-'}）· 证据覆盖 {Math.round(Number(evidence.evidence_coverage || 0) * 100)}%</p>
            {(evidence.strengths || []).length > 0 && <ul>{(evidence.strengths || []).map((item, i) => <li key={`s-${i}`}>{item}</li>)}</ul>}
            {(evidence.gaps || []).length > 0 && <ul>{(evidence.gaps || []).map((item, i) => <li key={`g-${i}`}>{item}</li>)}</ul>}
          </>
        ) : (
          <p className="reco-package-missing">{evidence.note || '暂无当前有效的判人评估，人岗匹配证据缺失'}</p>
        )}
      </section>
      <section>
        <h4>风险</h4>
        {risks.length ? <ul>{risks.map((item, i) => <li key={`r-${i}`}>{item}</li>)}</ul> : <p className="reco-package-missing">暂无风险记录</p>}
      </section>
      <section>
        <h4>待核验问题</h4>
        {questions.length ? <ul>{questions.map((item, i) => <li key={`q-${i}`}>{item}</li>)}</ul> : <p className="reco-package-missing">暂无待核验问题</p>}
      </section>
      <section>
        <h4>客户反馈（{feedback.length} 条）</h4>
        {feedback.map((item) => (
          <div className="reco-package-feedback-item" key={item.id}>
            <b>{item.feedback_type_label}</b>
            <span>{item.content}</span>
            <small>{date(item.feedback_time)} · {item.recorded_by || 'consultant'}</small>
          </div>
        ))}
        <div className="reco-package-feedback-form">
          <label>
            <span>反馈类型</span>
            <select aria-label="反馈类型" value={feedbackType} onChange={(event) => setFeedbackType(event.target.value)}>
              {feedbackTypeOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label>
            <span>反馈内容（必填）</span>
            <textarea aria-label="反馈内容" value={feedbackContent} onChange={(event) => setFeedbackContent(event.target.value)} placeholder="客户对人选/推荐包的反馈：认可点、顾虑、下一步安排…" rows={3} />
          </label>
          <button type="button" className="button primary" disabled={busy} onClick={submitFeedback}>
            {busy ? <LoaderCircle className="spin" /> : <MessageSquarePlus />}记录客户反馈
          </button>
          {receipt && (
            <div className={`candidate-action-feedback ${receipt.tone}`} role={receipt.tone === 'error' ? 'alert' : 'status'}>
              {receipt.tone === 'error' ? <TriangleAlert /> : <CircleCheck />}
              <span>{receipt.text}</span>
            </div>
          )}
        </div>
      </section>
    </div>
  )
}
