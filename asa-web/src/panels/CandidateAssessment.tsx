import { useCallback, useEffect, useState } from 'react'
import { CircleCheck, ClipboardCheck, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type {
  AssessmentAdvisorAction,
  AssessmentEvidence,
  CandidateAssessmentDoc,
  CandidateAssessmentPayload,
} from '../api'
import { humanizeActionError } from '../shared/errors'
import { date } from '../shared/format'
import { CalibrationMetrics } from './CalibrationMetrics'

// S6-1b 判人评估区（候选人详情「评估」tab，新文件；CandidatePanel 只最小接线）：
// 职业轨迹 + 跳槽质量史两维渲染、证据列表、置信度 tag、顾问口径摘要，
// 顾问动作行（采纳/改判/否决）经 PATCH advisor-action 幂等写回，响应直接回写本地。
// S6-3 补三块：「在同龄人里的位置」（band 中文 + 参照系 + 样本不足「推测」tag）、
// 「动机与时机」（信号列表带来源链接，无信号如实文案）、「需要核实的问题」（severity 三档色 + 证据，空态如实）。
// 文案一律 UX-1 业务语言（与后端 candidate_assessment.LABELS 同文）；红线：评估只辅助判断，不做决策，
// 风险一律以「需要核实的问题」呈现，不出现定罪/淘汰字眼。维度为 null（旧版评估）时对应区块不渲染。

const CONFIDENCE_LABELS: Record<string, string> = { certain: '确定', inferred: '推测' }
const PACE_LABELS: Record<string, string> = { fast: '偏快', normal: '正常', slow: '偏慢', unknown: '无法判断' }
const EVOLUTION_LABELS: Record<string, string> = { rising: '上升', lateral: '平移', stagnant: '吃老本', unknown: '无法判断' }
const DIRECTION_LABELS: Record<string, string> = { up: '上升', lateral: '平移', down: '下降', unknown: '无法判断' }
const TIER_LABELS: Record<string, string> = { T1: '头部', T2: '腰部', T3: '长尾', unknown: '未评级' }
const BAND_LABELS: Record<string, string> = { top10: '前 10%', top25: '前 25%', median: '中位区间', below: '相对靠后' }
const BASIS_LABELS: Record<string, string> = { fit_score: '既有评估得分', trajectory_features: '轨迹特征分' }
const SEVERITY_LABELS: Record<string, string> = { high: '高', medium: '中', low: '低' }
const RISK_KIND_LABELS: Record<string, string> = {
  gap: '空窗', frequent_hop: '频繁跳动', title_inflation: 'title 通胀',
  over_packaging: '过度包装信号', hard_requirement: '硬条件差距',
}
const ADVISOR_ACTION_LABELS: Record<AssessmentAdvisorAction, string> = {
  pending: '待处理', accepted: '已采纳', modified: '已改判', rejected: '已否决',
}

const confidenceLabel = (value?: string) => CONFIDENCE_LABELS[String(value || '')] || '推测'
const confidenceTone = (value?: string) => (value === 'certain' ? 'ok' : 'warn')
const directionLabel = (value?: string) => DIRECTION_LABELS[String(value || '')] || '无法判断'
const directionTone = (value?: string) => (value === 'up' ? 'ok' : value === 'down' ? 'warn' : 'muted')
const bandLabel = (value?: string | null) => (value ? BAND_LABELS[String(value)] || '无法落位' : '无法落位')
const severityLabel = (value?: string) => SEVERITY_LABELS[String(value || '')] || '低'
const severityTone = (value?: string) => (value === 'high' ? 'danger' : value === 'medium' ? 'warn' : 'muted')

const assessmentOf = (payload: CandidateAssessmentPayload | null): CandidateAssessmentDoc | null =>
  payload?.assessment && typeof payload.assessment === 'object' ? payload.assessment : null

// 证据归集：两维 evidence 合并渲染，标注维度中文名 + 类型（简历/图谱）+ ref。
const collectEvidence = (doc: CandidateAssessmentDoc): Array<{ dimension: string; item: AssessmentEvidence }> => {
  const dimensions = doc.dimensions || {}
  const groups: Array<{ name: string; label: string; items?: AssessmentEvidence[] }> = [
    { name: 'trajectory', label: '职业轨迹', items: dimensions.trajectory?.evidence },
    { name: 'move_history', label: '跳槽质量史', items: dimensions.move_history?.evidence },
  ]
  return groups.flatMap(group =>
    (group.items || []).map(item => ({ dimension: group.label, item })),
  )
}

export function CandidateAssessment({ candidateId, jobId }: { candidateId: number; jobId?: number }) {
  const [payload, setPayload] = useState<CandidateAssessmentPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState('')
  const [feedback, setFeedback] = useState<{ tone: 'error' | 'success'; text: string }>()
  const [noteOpen, setNoteOpen] = useState(false)
  const [noteDraft, setNoteDraft] = useState('')

  const fetchAssessment = useCallback(async () => {
    try {
      setPayload(await api.candidateAssessment(candidateId, jobId || 0))
    } catch (error) {
      setLoadError(humanizeActionError(error, '评估加载失败，请重试。'))
    } finally {
      setLoading(false)
    }
  }, [candidateId, jobId])
  useEffect(() => { void fetchAssessment() }, [fetchAssessment])
  const reload = () => {
    setLoading(true)
    setLoadError('')
    void fetchAssessment()
  }

  const generate = async () => {
    setBusy('generate')
    setFeedback(undefined)
    try {
      setPayload(await api.generateCandidateAssessment(candidateId, jobId || 0))
      setFeedback({ tone: 'success', text: '评估已生成。评估只辅助判断，最终判语由顾问本人下。' })
    } catch (error) {
      setFeedback({ tone: 'error', text: humanizeActionError(error, '评估生成失败，请重试。') })
    } finally {
      setBusy('')
    }
  }

  const act = async (action: AssessmentAdvisorAction, note?: string) => {
    setBusy(`action:${action}`)
    setFeedback(undefined)
    try {
      setPayload(await api.patchAssessmentAdvisorAction(candidateId, jobId || 0, action, note))
      setNoteOpen(false)
      setNoteDraft('')
      setFeedback({ tone: 'success', text: `${ADVISOR_ACTION_LABELS[action]}已记录，会回流用于评估校准。` })
    } catch (error) {
      setFeedback({ tone: 'error', text: humanizeActionError(error, '动作提交失败，请重试。') })
    } finally {
      setBusy('')
    }
  }

  if (!jobId) {
    return <section className="assessment" aria-label="判人评估"><div className="empty">缺少岗位上下文，无法加载评估。</div></section>
  }
  if (loading) {
    return (
      <section className="assessment" aria-label="判人评估">
        <div className="empty"><LoaderCircle className="spin" size={14}/> 评估加载中…</div>
      </section>
    )
  }
  if (loadError) {
    return (
      <section className="assessment" aria-label="判人评估">
        <div className="assessment-error" role="alert"><TriangleAlert/>{loadError}</div>
        <button className="button" onClick={reload}>重新加载</button>
      </section>
    )
  }

  const doc = assessmentOf(payload)
  if (!doc) {
    return (
      <section className="assessment" aria-label="判人评估">
        <div className="assessment-empty">
          <ClipboardCheck/>
          <div>
            <b>还没做过评估</b>
            <p>生成「职业轨迹 / 跳槽质量史 / 在同龄人里的位置 / 动机与时机 / 需要核实的问题」五维判语，辅助日常判人；评估只辅助判断，不构成决策建议。</p>
          </div>
          <button className="button primary" disabled={!!busy} onClick={() => void generate()}>
            {busy === 'generate' ? <LoaderCircle className="spin"/> : <ClipboardCheck/>}做评估
          </button>
        </div>
        {feedback?.tone === 'error' && <div className="assessment-error" role="alert"><TriangleAlert/>{feedback.text}</div>}
      </section>
    )
  }

  const trajectory = doc.dimensions?.trajectory || null
  const moveHistory = doc.dimensions?.move_history || null
  const percentile = doc.dimensions?.percentile || null
  const motivation = doc.dimensions?.motivation || null
  const risks = doc.dimensions?.risks || null
  const evidence = collectEvidence(doc)
  const advisorAction: AssessmentAdvisorAction = doc.advisor_action || 'pending'

  return (
    <section className="assessment" aria-label="判人评估">
      <div className="assessment-head">
        <div>
          <b>判人评估</b>
          <span>生成于 {date(doc.as_of)} · 评估只辅助判断，不构成决策建议</span>
        </div>
        <button className="button" disabled={!!busy} onClick={() => void generate()} title="按最新简历与策略重新生成，覆盖当前评估">
          {busy === 'generate' ? <LoaderCircle className="spin"/> : <RefreshCw/>}重新评估
        </button>
      </div>
      {feedback && (
        <div className={`assessment-feedback ${feedback.tone}`} role={feedback.tone === 'error' ? 'alert' : 'status'}>
          {feedback.tone === 'error' ? <TriangleAlert/> : <CircleCheck/>}<span>{feedback.text}</span>
        </div>
      )}

      {trajectory && (
        <section className="assessment-dim" aria-label="职业轨迹">
          <div className="assessment-dim-head">
            <h3>职业轨迹</h3>
            <span className={`tag ${confidenceTone(trajectory.confidence)}`}>{confidenceLabel(trajectory.confidence)}</span>
          </div>
          {trajectory.verdict && <p className="assessment-verdict">{trajectory.verdict}</p>}
          <div className="assessment-facts">
            <span>晋升速度：<b>{PACE_LABELS[String(trajectory.promotion_pace || '')] || '无法判断'}</b></span>
            <span>技术栈演进：<b>{EVOLUTION_LABELS[String(trajectory.tech_evolution || '')] || '无法判断'}</b></span>
          </div>
          {(trajectory.segments || []).length > 0 && (
            <table className="assessment-segments">
              <thead><tr><th>期间</th><th>公司</th><th>职位</th><th>平台含金量</th></tr></thead>
              <tbody>
                {(trajectory.segments || []).map((segment, index) => (
                  <tr key={`${segment.period || ''}-${segment.company || ''}-${index}`}>
                    <td>{segment.period || '-'}</td>
                    <td>{segment.company || '-'}</td>
                    <td>{segment.title || '-'}</td>
                    <td>
                      {TIER_LABELS[String(segment.tier || '')] || '未评级'}
                      {segment.tier_source === 'inferred' && <span className="tag warn">推测</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {moveHistory && (
        <section className="assessment-dim" aria-label="跳槽质量史">
          <div className="assessment-dim-head">
            <h3>跳槽质量史</h3>
            <span className={`tag ${confidenceTone(moveHistory.confidence)}`}>{confidenceLabel(moveHistory.confidence)}</span>
          </div>
          {moveHistory.verdict && <p className="assessment-verdict">{moveHistory.verdict}</p>}
          <div className="assessment-moves">
            {(moveHistory.moves || []).map((move, index) => (
              <div className="assessment-move" key={`${move.from || ''}-${move.to || ''}-${index}`}>
                <div className="assessment-move-head">
                  <b>{move.from || '上一段'} → {move.to || '下一段'}</b>
                  <span className={`tag ${directionTone(move.direction)}`}>{directionLabel(move.direction)}</span>
                </div>
                {move.reason && <p>{move.reason}</p>}
              </div>
            ))}
          </div>
          <div className="assessment-current-move">
            当前这单判定：<b>{directionLabel(moveHistory.current_move)}</b>
          </div>
        </section>
      )}

      {percentile && (
        <section className="assessment-dim" aria-label="在同龄人里的位置">
          <div className="assessment-dim-head">
            <h3>在同龄人里的位置</h3>
            {percentile.reference && percentile.reference.sample_sufficient === false && (
              <span className="tag warn">推测 · 参照样本不足</span>
            )}
            <span className={`tag ${confidenceTone(percentile.confidence)}`}>{confidenceLabel(percentile.confidence)}</span>
          </div>
          {percentile.verdict && <p className="assessment-verdict">{percentile.verdict}</p>}
          <div className="assessment-facts">
            <span>落位：<b>{bandLabel(percentile.band)}</b></span>
            {percentile.score !== null && percentile.score !== undefined && (
              <span>得分：<b>{percentile.score}</b>（{BASIS_LABELS[String(percentile.basis || '')] || percentile.basis || '轨迹特征分'}）</span>
            )}
          </div>
          {percentile.reference && (
            <div className="assessment-facts">
              <span>
                参照系：同方向{percentile.reference.direction ? `（${percentile.reference.direction}）` : ''}
                {percentile.reference.years_window !== null && percentile.reference.years_window !== undefined
                  ? `±${percentile.reference.years_window}年` : '不限年限'}
                ｜样本 N=<b>{percentile.reference.n ?? 0}</b>
                {percentile.reference.median !== null && percentile.reference.median !== undefined && (
                  <>｜中位分 {percentile.reference.median}（P25={percentile.reference.q25}，P75={percentile.reference.q75}）</>
                )}
              </span>
            </div>
          )}
          {percentile.reference?.note && <p className="assessment-note-line">{percentile.reference.note}</p>}
        </section>
      )}

      {motivation && (
        <section className="assessment-dim" aria-label="动机与时机">
          <div className="assessment-dim-head">
            <h3>动机与时机</h3>
            <span className={`tag ${confidenceTone(motivation.confidence)}`}>{confidenceLabel(motivation.confidence)}</span>
          </div>
          {motivation.verdict && <p className="assessment-verdict">{motivation.verdict}</p>}
          {(motivation.signals || []).length > 0 ? (
            <ul className="assessment-signals">
              {(motivation.signals || []).map((signal, index) => (
                <li key={`${signal.kind || 'signal'}-${index}`}>
                  <span className="tag muted">{signal.source || '信号'}</span>
                  <span>{signal.summary || ''}</span>
                  {signal.url && (
                    <a href={signal.url} target="_blank" rel="noreferrer">
                      来源
                    </a>
                  )}
                  {signal.as_of && <span className="assessment-signal-date">{signal.as_of}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="assessment-note-line">未见明显变动信号：动机与时机需面谈核实。</p>
          )}
        </section>
      )}

      {risks && (
        <section className="assessment-dim" aria-label="需要核实的问题">
          <div className="assessment-dim-head">
            <h3>需要核实的问题</h3>
            <span className={`tag ${confidenceTone(risks.confidence)}`}>{confidenceLabel(risks.confidence)}</span>
          </div>
          {risks.verdict && <p className="assessment-verdict">{risks.verdict}</p>}
          {(risks.items || []).length > 0 ? (
            <ul className="assessment-risks">
              {(risks.items || []).map((item, index) => (
                <li key={`${item.kind || 'risk'}-${index}`}>
                  <div className="assessment-risk-head">
                    <span className={`tag ${severityTone(item.severity)}`}>{severityLabel(item.severity)}</span>
                    {item.kind && <span className="tag muted">{RISK_KIND_LABELS[String(item.kind)] || item.kind}</span>}
                    <span>{item.risk || ''}</span>
                  </div>
                  {(item.evidence || []).length > 0 && (
                    <ul className="assessment-risk-evidence">
                      {(item.evidence || []).map((ev, evIndex) => (
                        <li key={evIndex}>
                          <span className="tag">{ev.type || '未标注'}</span>
                          <span>{ev.ref || ''}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="assessment-note-line">未见需核实的问题。</p>
          )}
          <p className="assessment-note-line">以上是辅助核实的清单，不构成任何决策建议。</p>
        </section>
      )}

      {doc.consultant_summary && (
        <section className="assessment-dim" aria-label="顾问口径摘要">
          <div className="assessment-dim-head"><h3>顾问口径摘要</h3></div>
          <p className="assessment-summary">{doc.consultant_summary}</p>
        </section>
      )}

      {evidence.length > 0 && (
        <section className="assessment-dim" aria-label="证据">
          <div className="assessment-dim-head"><h3>证据</h3><span>{evidence.length} 条</span></div>
          <ul className="assessment-evidence">
            {evidence.map(({ dimension, item }, index) => (
              <li key={`${dimension}-${index}`}>
                <span className="tag muted">{dimension}</span>
                <span className="tag">{item.type || '未标注'}</span>
                <span>{item.ref || ''}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="assessment-actions" aria-label="顾问动作">
        <div className="assessment-actions-row">
          <span>顾问动作：{ADVISOR_ACTION_LABELS[advisorAction]}</span>
          <button
            className={`button${advisorAction === 'accepted' ? ' primary' : ''}`}
            disabled={!!busy}
            onClick={() => void act('accepted')}
          >
            {busy === 'action:accepted' ? <LoaderCircle className="spin"/> : <CircleCheck/>}采纳
          </button>
          <button
            className={`button${advisorAction === 'modified' ? ' primary' : ''}`}
            disabled={!!busy}
            onClick={() => { setNoteOpen(open => !open); setNoteDraft(doc.advisor_note || '') }}
          >
            改判
          </button>
          <button
            className={`button${advisorAction === 'rejected' ? ' danger' : ''}`}
            disabled={!!busy}
            onClick={() => void act('rejected')}
          >
            {busy === 'action:rejected' ? <LoaderCircle className="spin"/> : null}否决
          </button>
        </div>
        {noteOpen && (
          <div className="assessment-note">
            <textarea
              aria-label="改判口径备注"
              value={noteDraft}
              onChange={event => setNoteDraft(event.target.value)}
              placeholder="写下你的判定口径，会回流用于评估校准…"
              rows={3}
            />
            <button
              className="button primary"
              disabled={!!busy || !noteDraft.trim()}
              onClick={() => void act('modified', noteDraft.trim())}
            >
              {busy === 'action:modified' ? <LoaderCircle className="spin"/> : null}提交改判
            </button>
          </div>
        )}
        {advisorAction !== 'pending' && doc.advisor_note && (
          <p className="assessment-advisor-note">顾问备注：{doc.advisor_note}</p>
        )}
      </div>

      <CalibrationMetrics/>
    </section>
  )
}
