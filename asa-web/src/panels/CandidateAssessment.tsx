import { useCallback, useEffect, useRef, useState } from 'react'
import { CircleCheck, ClipboardCheck, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type {
  AssessmentAdvisorAction,
  AssessmentEvidence,
  CandidateAssessmentDoc,
  CandidateAssessmentFit,
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
// 2026-08-13 简洁化重构：摘要优先——匹配点分析收成速读摘要行（评分/等级/建议内联，强弱点各露前 2 条，
// criteria 分组默认全收起）；五维默认只露标题 + 置信度 tag + verdict，明细/证据统一收进原生 <details>；
// 「需要核实的问题」条目列表保持可见、条目证据默认收起；顾问动作上移到匹配点分析之后；
// 证据区整包一层 <details> 默认收起。折叠一律用原生 <details>/<summary>，不再维护本地折叠 state。
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
// 匹配点分析（人岗匹配评估 criteria 分组/状态文案）：只读展示，状态枚举与后端 scoring.STATUS_VALUE 一致。
const FIT_GROUP_LABELS: Record<string, string> = {
  hard_requirements: '硬门槛', core_abilities: '核心能力', soft_preferences: '软偏好',
}
const FIT_STATUS_LABELS: Record<string, string> = { met: '满足', partial: '部分满足', not_met: '不满足', unknown: '待核验' }
// 初评（candidate_intelligence）历史数据存在英文/混杂枚举，前端兜底转中文；中文枚举原样直通。
const FIT_LEVEL_LABELS: Record<string, string> = {
  'A-优先推进': 'A-优先推进', 'B-可推进': 'B-可推进', 'C-需确认': 'C-需确认', 'D-暂缓': 'D-暂缓',
  unrated: '未评级', medium: '中等', strong: '强', hold: '暂缓', 不匹配: '不匹配',
}
const FIT_RECOMMENDATION_LABELS: Record<string, string> = {
  '建议不推进': '建议不推进', '先核验后判断': '先核验后判断', '建议优先复核': '建议优先复核', '暂缓': '暂缓',
  pending_review: '待复核', ready_for_review: '待复核', needs_review: '待复核', recommended: '推荐推进', contacted: '已联系',
  continue_pending_manual_contact: '待人工跟进', backup: '备选', shortlist: '入围', hold: '暂缓', reject: '不推进',
  age_risk_review: '年龄风险待核',
}

const confidenceLabel = (value?: string) => CONFIDENCE_LABELS[String(value || '')] || '推测'
const confidenceTone = (value?: string) => (value === 'certain' ? 'ok' : 'warn')
const directionLabel = (value?: string) => DIRECTION_LABELS[String(value || '')] || '无法判断'
const directionTone = (value?: string) => (value === 'up' ? 'ok' : value === 'down' ? 'warn' : 'muted')
const bandLabel = (value?: string | null) => (value ? BAND_LABELS[String(value)] || '无法落位' : '无法落位')
const severityLabel = (value?: string) => SEVERITY_LABELS[String(value || '')] || '低'
const severityTone = (value?: string) => (value === 'high' ? 'danger' : value === 'medium' ? 'warn' : 'muted')
const fitStatusLabel = (value?: string) => FIT_STATUS_LABELS[String(value || '')] || '待核验'
const fitStatusTone = (value?: string) => (value === 'met' ? 'ok' : value === 'partial' ? 'warn' : value === 'not_met' ? 'danger' : 'muted')
const fitLevelLabel = (value?: string) => (value ? FIT_LEVEL_LABELS[String(value)] || String(value) : '')
const fitRecommendationLabel = (value?: string) => (value ? FIT_RECOMMENDATION_LABELS[String(value)] || String(value) : '')
const fitScoreTone = (score?: number) =>
  score === null || score === undefined ? 'muted' : score >= 70 ? 'ok' : score >= 55 ? 'warn' : 'danger'
// 初评来源的 fit_score=0 是「未评分」占位（历史数据未落分），不能当作 0 分误导；深度评估的 0 分为真实「不匹配」。
const isInitialFit = (fit: CandidateAssessmentFit) => fit.source === 'candidate_intelligence'
const fitScoreShown = (fit: CandidateAssessmentFit) =>
  fit.fit_score === null || fit.fit_score === undefined || (isInitialFit(fit) && fit.fit_score === 0) ? null : fit.fit_score

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

// S6-5 三桶证据卡（一期缺口）：证据只有维度级 confidence（无条目级），按保守口径归桶——
// 直接证据=维度置信度 certain 且带出处；合理推断=inferred 且带出处；
// 未知项=缺出处、置信度缺失/未知的证据，以及「需要核实的问题」里的条目（该块本质待核验，不进前两桶）。
// 只读现有字段、不改后端契约；无法判断的一律进未知项，口径在视图内如实说明。
type EvidenceBucketKey = 'direct' | 'inferred' | 'unknown'
type EvidenceCardItem = { dimension: string; type: string; ref: string; bucket: EvidenceBucketKey }
const EVIDENCE_BUCKET_ORDER: EvidenceBucketKey[] = ['direct', 'inferred', 'unknown']
const EVIDENCE_BUCKET_LABELS: Record<EvidenceBucketKey, string> = { direct: '直接证据', inferred: '合理推断', unknown: '未知项' }

const evidenceBucketOf = (confidence: string | undefined, hasRef: boolean): EvidenceBucketKey => {
  if (!hasRef) return 'unknown'
  if (confidence === 'certain') return 'direct'
  if (confidence === 'inferred') return 'inferred'
  return 'unknown'
}

const collectEvidenceCards = (doc: CandidateAssessmentDoc): EvidenceCardItem[] => {
  const dimensions = doc.dimensions || {}
  const groups: Array<{ label: string; confidence?: string; items?: AssessmentEvidence[] }> = [
    { label: '职业轨迹', confidence: dimensions.trajectory?.confidence, items: dimensions.trajectory?.evidence },
    { label: '跳槽质量史', confidence: dimensions.move_history?.confidence, items: dimensions.move_history?.evidence },
    { label: '在同龄人里的位置', confidence: dimensions.percentile?.confidence, items: dimensions.percentile?.evidence },
    { label: '动机与时机', confidence: dimensions.motivation?.confidence, items: dimensions.motivation?.evidence },
  ]
  const cards = groups.flatMap(group =>
    (group.items || []).map(item => ({
      dimension: group.label,
      type: item.type || '未标注',
      ref: item.ref || '未提供出处',
      bucket: evidenceBucketOf(group.confidence, Boolean(item.ref)),
    })),
  )
  for (const item of dimensions.risks?.items || []) {
    cards.push({
      dimension: '需要核实的问题',
      type: `待核验 · ${severityLabel(item.severity)}风险`,
      ref: item.risk || '未说明待核验内容',
      bucket: 'unknown',
    })
  }
  return cards
}

// 匹配点分析（2026-08-13 简洁化）：速读摘要行 = 匹配评分 + 等级 tag + 推进建议 + 证据覆盖 + 标准满足度统计，
// 一行看结论；强/弱匹配点两栏各露前 2 条，其余收进 <details>；逐条标准分组全部默认收起（summary 含满足计数）。
// 数据来源：Agent 深度评估（criteria 逐条）或候选人匹配初评（强/弱匹配项），只读展示，不触发新生成。
const FIT_PREVIEW_COUNT = 2

function MatchAnalysisSection({ fit, onRefreshFit, refreshBusy, refreshDisabled }: {
  fit: CandidateAssessmentFit
  onRefreshFit?: () => void
  refreshBusy?: boolean
  refreshDisabled?: boolean
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const strengths = fit.strengths || []
  const gaps = fit.gaps || []
  const initial = isInitialFit(fit)
  const score = fitScoreShown(fit)
  const level = fitLevelLabel(fit.fit_level)
  const recommendation = fitRecommendationLabel(fit.recommendation_label)
  const groups = Object.entries(FIT_GROUP_LABELS)
    .map(([key, label]) => ({ key, label, rows: fit.criteria?.[key] || [] }))
    .filter(group => group.rows.length > 0)
  const tally = { met: 0, partial: 0, not_met: 0, unknown: 0 }
  for (const group of groups) {
    for (const item of group.rows) {
      const status = item.status === 'met' || item.status === 'partial' || item.status === 'not_met' ? item.status : 'unknown'
      tally[status] += 1
    }
  }
  const toggle = (id: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const renderPoints = (items: string[], kind: 'strength' | 'gap') => {
    const label = kind === 'strength' ? '强匹配点' : '弱匹配点'
    return (
      <>
        <ul>{items.slice(0, FIT_PREVIEW_COUNT).map((item, index) => <li key={`fit-${kind}-${index}`}>{item}</li>)}</ul>
        {items.length > FIT_PREVIEW_COUNT && (
          <details className="assessment-detail assessment-fit-more">
            <summary>全部{label} · {items.length} 条</summary>
            <ul>{items.slice(FIT_PREVIEW_COUNT).map((item, index) => <li key={`fit-${kind}-more-${index}`}>{item}</li>)}</ul>
          </details>
        )}
      </>
    )
  }
  return (
    <section className="assessment-dim assessment-fit" aria-label="匹配点分析">
      <div className="assessment-dim-head">
        <h3>匹配点分析</h3>
        <span className={`tag ${initial ? 'muted' : 'ok'}`}>{initial ? '匹配初评' : '深度评估'}</span>
        {fit.created_at && <span>评估于 {date(fit.created_at)}</span>}
        {onRefreshFit && (
          <button
            className="button assessment-fit-refresh"
            disabled={refreshDisabled || refreshBusy}
            onClick={onRefreshFit}
            title="按最新简历与岗位重跑人岗匹配评估，通常需要几十秒"
          >
            {refreshBusy ? <LoaderCircle className="spin"/> : <RefreshCw/>}
            {initial ? '生成深度评估' : '重新评估匹配'}
          </button>
        )}
      </div>
      <div className="assessment-fit-summary">
        {score === null
          ? <span className="tag muted">未评分</span>
          : <span>匹配评分 <b>{score}</b></span>}
        {level && <span className={`tag ${fitScoreTone(fit.fit_score)}`}>{level}</span>}
        {recommendation && <span>推进建议：<b>{recommendation}</b></span>}
        {fit.evidence_coverage !== null && fit.evidence_coverage !== undefined && (
          <span>证据覆盖：<b>{Math.round(Number(fit.evidence_coverage) * 100)}%</b></span>
        )}
        {groups.length > 0 && (
          <span>
            标准满足度：<b>{tally.met}</b> 满足 · <b>{tally.partial}</b> 部分 · <b>{tally.not_met}</b> 不满足 · <b>{tally.unknown}</b> 待核验
          </span>
        )}
      </div>
      {strengths.length === 0 && gaps.length === 0 ? (
        <p className="assessment-note-line">
          {initial ? '初评未提供逐条强/弱匹配点，可结合下方判人评估五维判断。' : '暂无强/弱匹配点条目。'}
        </p>
      ) : (
        <div className="assessment-fit-columns">
          <div className="assessment-fit-col">
            <h4><span className="assessment-fit-dot ok"/>强匹配点<span>{strengths.length} 条</span></h4>
            {strengths.length > 0 ? renderPoints(strengths, 'strength') : (
              <p className="assessment-note-line">暂无明确强匹配点。</p>
            )}
          </div>
          <div className="assessment-fit-col">
            <h4><span className="assessment-fit-dot warn"/>弱匹配点 / 缺口<span>{gaps.length} 条</span></h4>
            {gaps.length > 0 ? renderPoints(gaps, 'gap') : (
              <p className="assessment-note-line">暂无明显缺口。</p>
            )}
          </div>
        </div>
      )}
      {groups.map(group => {
        const metCount = group.rows.filter(item => item.status === 'met').length
        return (
          <details className="assessment-fit-group" key={`fit-group-${group.key}`}>
            <summary>{group.label}<span>满足 {metCount}/{group.rows.length}</span></summary>
            <ul>
              {group.rows.map((item, index) => {
                const id = `${group.key}-${index}`
                const detail = [item.reason, ...(item.evidence || [])].filter(Boolean).join('；')
                return (
                  <li key={id}>
                    <span className={`assessment-fit-dot ${fitStatusTone(item.status)}`} title={fitStatusLabel(item.status)}/>
                    <div className="assessment-fit-criterion">
                      <button
                        type="button"
                        className={`assessment-fit-criterion-text${expanded.has(id) ? ' open' : ''}`}
                        onClick={() => toggle(id)}
                        title={expanded.has(id) ? '点击收起' : '点击展开全文'}
                      >
                        {item.criterion || '未命名标准'}
                      </button>
                      {detail && <p>{detail}</p>}
                    </div>
                    <span className={`tag ${fitStatusTone(item.status)}`}>{fitStatusLabel(item.status)}</span>
                  </li>
                )
              })}
            </ul>
          </details>
        )
      })}
      <p className="assessment-note-line">
        {initial
          ? '匹配点来自候选人匹配初评（强/弱匹配项）。'
          : '匹配点来自 ASA 当前人岗匹配评估。'}
      </p>
    </section>
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
  const [evidenceView, setEvidenceView] = useState<'buckets' | 'dimensions'>('buckets')
  // 异步防串台：切换人选后，晚到的旧请求/慢速生成响应一律丢弃，不覆盖当前人选的数据。
  const requestSeq = useRef(0)

  const fetchAssessment = useCallback(async () => {
    const seq = ++requestSeq.current
    setBusy('')
    // 切换人选/重新加载时先回加载态，避免旧人选数据在屏幕上短暂残留（串台闪烁）。
    setLoading(true)
    setLoadError('')
    try {
      const result = await api.candidateAssessment(candidateId, jobId || 0)
      if (seq !== requestSeq.current) return
      setPayload(result)
    } catch (error) {
      if (seq !== requestSeq.current) return
      setLoadError(humanizeActionError(error, '评估加载失败，请重试。'))
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [candidateId, jobId])
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void fetchAssessment() }, [fetchAssessment])
  const reload = () => {
    if (loading) return
    setLoading(true)
    setLoadError('')
    void fetchAssessment()
  }

  // 写操作（生成/顾问动作）响应不含 fit 块：合并上一份 payload 里的 fit，避免写回后匹配点分析消失。
  const applyWritePayload = (result: CandidateAssessmentPayload, seq: number) => {
    if (seq !== requestSeq.current) return false
    setPayload(prev => ({ ...result, fit: result.fit !== undefined ? result.fit : prev?.fit ?? null }))
    return true
  }

  const generate = async () => {
    const seq = ++requestSeq.current
    setBusy('generate')
    setFeedback(undefined)
    const force = Boolean(assessmentOf(payload))
    try {
      const result = await api.generateCandidateAssessment(candidateId, jobId || 0, force)
      if (!applyWritePayload(result, seq)) return
      setFeedback({ tone: 'success', text: `${force ? '评估已重新生成' : '评估已生成'}。评估只辅助判断，最终判语由顾问本人下。` })
    } catch (error) {
      if (seq !== requestSeq.current) return
      setFeedback({ tone: 'error', text: humanizeActionError(error, '评估生成失败，请重试。') })
    } finally {
      if (seq === requestSeq.current) setBusy('')
    }
  }

  const act = async (action: AssessmentAdvisorAction, note?: string) => {
    const seq = ++requestSeq.current
    setBusy(`action:${action}`)
    setFeedback(undefined)
    try {
      const result = await api.patchAssessmentAdvisorAction(candidateId, jobId || 0, action, note)
      if (!applyWritePayload(result, seq)) return
      setNoteOpen(false)
      setNoteDraft('')
      setFeedback({ tone: 'success', text: `${ADVISOR_ACTION_LABELS[action]}已记录，会回流用于评估校准。` })
    } catch (error) {
      if (seq !== requestSeq.current) return
      setFeedback({ tone: 'error', text: humanizeActionError(error, '动作提交失败，请重试。') })
    } finally {
      if (seq === requestSeq.current) setBusy('')
    }
  }

  // 重新评估匹配：强制重跑 Agent 人岗匹配评估；响应带最新 fit，直接合并进当前 payload（不闪加载态）。
  const refreshFit = async () => {
    const seq = ++requestSeq.current
    setBusy('fit')
    setFeedback(undefined)
    try {
      const result = await api.refreshFitAssessment(candidateId, jobId || 0)
      if (seq !== requestSeq.current) return
      if (result.fit) setPayload(prev => ({ ...(prev || { ok: true }), fit: result.fit }))
      setFeedback({ tone: 'success', text: '匹配评估已重新生成。' })
    } catch (error) {
      if (seq !== requestSeq.current) return
      setFeedback({ tone: 'error', text: humanizeActionError(error, '匹配评估生成失败，请重试。') })
    } finally {
      if (seq === requestSeq.current) setBusy('')
    }
  }

  if (!jobId) {
    return <section className="assessment" aria-label="判人评估"><div className="empty">缺少岗位上下文，无法加载评估。</div></section>
  }
  if (loading) {
    return (
      <section className="assessment" aria-label="判人评估" aria-busy="true">
        <div className="assessment-loading" role="status"><LoaderCircle className="spin" size={14}/> 评估加载中…</div>
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
        {payload?.fit ? (
          <MatchAnalysisSection
            fit={payload.fit}
            onRefreshFit={() => void refreshFit()}
            refreshBusy={busy === 'fit'}
            refreshDisabled={Boolean(busy) && busy !== 'fit'}
          />
        ) : null}
        <div className="assessment-empty">
          <ClipboardCheck/>
          <div>
            <b>还没做过评估</b>
            <p>一键生成五维判语（职业轨迹 / 跳槽质量 / 同龄位置 / 动机 / 待核实问题），辅助判人，不构成决策建议。</p>
          </div>
          <button className="button primary" disabled={!!busy} onClick={() => void generate()}>
            {busy === 'generate' ? <LoaderCircle className="spin"/> : <ClipboardCheck/>}做评估
          </button>
        </div>
        {busy === 'generate' && (
          <div className="assessment-generating">
            <LoaderCircle className="spin" size={14}/>
            <span>正在生成评估，模型处理通常需要几十秒；期间可以切换查看其他人选，结果会自动保存。</span>
          </div>
        )}
        {busy === 'fit' && (
          <div className="assessment-generating">
            <LoaderCircle className="spin" size={14}/>
            <span>正在生成深度匹配评估，通常需要几十秒；期间可以切换查看其他人选，结果会自动保存。</span>
          </div>
        )}
        {feedback?.tone === 'error' && <div className="assessment-error" role="alert"><TriangleAlert/>{feedback.text}</div>}
        {feedback?.tone === 'success' && <div className="assessment-feedback success" role="status"><CircleCheck/><span>{feedback.text}</span></div>}
      </section>
    )
  }

  const trajectory = doc.dimensions?.trajectory || null
  const moveHistory = doc.dimensions?.move_history || null
  const percentile = doc.dimensions?.percentile || null
  const motivation = doc.dimensions?.motivation || null
  const risks = doc.dimensions?.risks || null
  const evidence = collectEvidence(doc)
  const evidenceGroups = evidence.reduce<Array<{ dimension: string; items: AssessmentEvidence[] }>>((groups, { dimension, item }) => {
    const group = groups.find(candidate => candidate.dimension === dimension)
    if (group) group.items.push(item)
    else groups.push({ dimension, items: [item] })
    return groups
  }, [])
  const evidenceCards = collectEvidenceCards(doc)
  const evidenceBuckets = EVIDENCE_BUCKET_ORDER.map(key => ({ key, items: evidenceCards.filter(card => card.bucket === key) }))
  const advisorAction: AssessmentAdvisorAction = doc.advisor_action || 'pending'
  const fit = payload?.fit || null
  // 无明细内容的维度不渲染「明细」折叠（点进去是空的）。
  const trajectoryHasDetail = Boolean(trajectory && ((trajectory.segments || []).length > 0
    || (trajectory.promotion_pace && trajectory.promotion_pace !== 'unknown')
    || (trajectory.tech_evolution && trajectory.tech_evolution !== 'unknown')))
  const moveHistoryHasDetail = Boolean(moveHistory && ((moveHistory.moves || []).length > 0
    || (moveHistory.current_move && moveHistory.current_move !== 'unknown')))

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
      {busy === 'generate' && (
        <div className="assessment-generating">
          <LoaderCircle className="spin" size={14}/>
          <span>正在{assessmentOf(payload) ? '重新' : ''}生成评估，模型处理通常需要几十秒；期间可以切换查看其他人选，结果会自动保存。</span>
        </div>
      )}
      {busy === 'fit' && (
        <div className="assessment-generating">
          <LoaderCircle className="spin" size={14}/>
          <span>正在重新生成匹配评估，通常需要几十秒；期间可以切换查看其他人选，结果会自动保存。</span>
        </div>
      )}

      {fit && (
        <MatchAnalysisSection
          fit={fit}
          onRefreshFit={() => void refreshFit()}
          refreshBusy={busy === 'fit'}
          refreshDisabled={Boolean(busy) && busy !== 'fit'}
        />
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

      {trajectory && (
        <section className="assessment-dim" aria-label="职业轨迹">
          <div className="assessment-dim-head">
            <h3>职业轨迹</h3>
            <span className={`tag ${confidenceTone(trajectory.confidence)}`}>{confidenceLabel(trajectory.confidence)}</span>
          </div>
          {trajectory.verdict && <p className="assessment-verdict">{trajectory.verdict}</p>}
          {trajectoryHasDetail && (
          <details className="assessment-detail">
            <summary>明细</summary>
            <div className="assessment-detail-body">
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
            </div>
          </details>
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
          {moveHistoryHasDetail && (
          <details className="assessment-detail">
            <summary>明细</summary>
            <div className="assessment-detail-body">
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
            </div>
          </details>
          )}
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
          <details className="assessment-detail">
            <summary>明细</summary>
            <div className="assessment-detail-body">
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
            </div>
          </details>
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
            <details className="assessment-detail">
              <summary>明细</summary>
              <div className="assessment-detail-body">
                <ul className="assessment-signals">
                  {(motivation.signals || []).map((signal, index) => (
                    <li key={`${signal.kind || 'signal'}-${index}`}>
                      <span className="tag muted">{signal.source || '信号'}</span>
                      <span className="assessment-signal-summary" title={signal.summary || undefined}>{signal.summary || ''}</span>
                      {signal.url && (
                        <a href={signal.url} target="_blank" rel="noreferrer">
                          来源
                        </a>
                      )}
                      {signal.as_of && <span className="assessment-signal-date">{signal.as_of}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            </details>
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
                    <details className="assessment-detail assessment-risk-detail">
                      <summary>证据明细</summary>
                      <ul className="assessment-risk-evidence">
                        {(item.evidence || []).map((ev, evIndex) => (
                          <li key={evIndex}>
                            <span className="tag">{ev.type || '未标注'}</span>
                            <span className="assessment-evidence-ref" title={ev.ref || undefined}>{ev.ref || '未提供出处'}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
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
          <div className="assessment-dim-head">
            <h3>顾问口径摘要</h3>
          </div>
          <p className="assessment-summary">{doc.consultant_summary}</p>
        </section>
      )}

      {evidenceCards.length > 0 && (
        <section className="assessment-dim" aria-label="证据">
          <details className="assessment-detail assessment-evidence-block">
            <summary>证据 · {evidenceCards.length} 条</summary>
            <div className="assessment-detail-body">
              <div className="assessment-evidence-toolbar">
                <span>{evidenceView === 'buckets' ? `${evidenceCards.length} 条 · 按三桶归集` : `${evidence.length} 条 · 按维度分组`}</span>
                <div className="assessment-evidence-toggle" role="group" aria-label="证据视图切换">
                  <button
                    type="button"
                    className={evidenceView === 'buckets' ? 'active' : ''}
                    aria-pressed={evidenceView === 'buckets'}
                    onClick={() => setEvidenceView('buckets')}
                  >三桶视图</button>
                  <button
                    type="button"
                    className={evidenceView === 'dimensions' ? 'active' : ''}
                    aria-pressed={evidenceView === 'dimensions'}
                    onClick={() => setEvidenceView('dimensions')}
                  >按维度</button>
                </div>
              </div>
              {evidenceView === 'buckets' ? (
                <>
                  <p className="assessment-note-line">归桶口径：维度置信度「确定」且带出处的进直接证据；「推测」的进合理推断；缺出处、置信度未知的证据，以及「需要核实的问题」里的待核验条目，一律进未知项。</p>
                  {evidenceBuckets.map(bucket => (
                    <div className="assessment-evidence-group" key={bucket.key}>
                      <h4>{EVIDENCE_BUCKET_LABELS[bucket.key]}<span>{bucket.items.length} 条</span></h4>
                      {bucket.items.length > 0 ? (
                        <ul className="assessment-evidence">
                          {bucket.items.map((card, index) => (
                            <li key={`${bucket.key}-${index}`}>
                              <span className="tag muted">{card.dimension}</span>
                              <span className="tag">{card.type}</span>
                              <span className="assessment-evidence-ref" title={card.ref}>{card.ref}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="assessment-note-line">本桶暂无条目。</p>
                      )}
                    </div>
                  ))}
                </>
              ) : evidence.length > 0 ? (
                evidenceGroups.map(group => (
                  <div className="assessment-evidence-group" key={group.dimension}>
                    <h4>{group.dimension}<span>{group.items.length} 条</span></h4>
                    <ul className="assessment-evidence">
                      {group.items.map((item, index) => (
                        <li key={`${group.dimension}-${index}`}>
                          <span className="tag">{item.type || '未标注'}</span>
                          <span className="assessment-evidence-ref" title={item.ref || undefined}>{item.ref || '未提供出处'}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))
              ) : (
                <p className="assessment-note-line">暂无按维度归集的证据。</p>
              )}
            </div>
          </details>
        </section>
      )}

      <CalibrationMetrics/>
    </section>
  )
}
