import { useCallback, useEffect, useState } from 'react'
import { ClipboardCheck, LoaderCircle, RefreshCw, UserRoundSearch } from 'lucide-react'
import { api } from '../api'
import type { ChannelDownweight, EvaluationReviewItem, StrategyReviewChannelFinding, StrategyReviewDiff, StrategyReviewPayload } from '../api'
import { humanizeActionError } from '../shared/errors'
import { channelLabel } from './utils'
import { diffContentText, diffOpLabel, diffStepLabel } from './strategyReviewDiff'
import { StrategyReviewExpansion } from './StrategyReviewExpansion'

// S4-3 策略复盘：终局工作流（completed/blocked/failed）展示规则版复盘结论与修订建议 diff。
// 独立按需路由 /strategy-review，面板挂载与详情刷新（updatedAt 变化）时拉取，不进 /summary 轮询签名。
// 无复盘（404）显示"该轮未生成策略复盘"并提供"生成复盘"（调 rebuild 幂等重算）。
// degraded / insufficient_data 按后端语义如实呈现（证据不完整仅供参考），不夸大结论。
// 策略调整统一在 Copilot 确认卡完成；主面板只展示复盘证据与后端已记录的状态。
// S4-5：N4 渠道降权建议（channel_downweights，仅建议不执行）与 N5 评估尺度复核（evaluation_review）
// 如实渲染；尺度复核条目可跳候选人详情页，经"评分复核"快捷记录回写顾问结论（既有 commit 链路）。

const REVIEWABLE_STATUSES = new Set(['completed', 'blocked', 'failed'])

const FINDING_LABELS: Record<string, string> = {
  execution_issue: '执行/渠道',
  zero_recall: '零召回',
  low_high_rate: '高分率低',
}

export function StrategyReview({ workflowId, status, updatedAt, openCandidate, jobId, mappingArtifactId, onOpenMapping }: {
  workflowId: string
  status: string
  updatedAt: string
  openCandidate?: (id: number) => void
  jobId?: number
  mappingArtifactId?: string
  onOpenMapping?: (artifactId: string) => void
}) {
  const reviewable = REVIEWABLE_STATUSES.has(status)
  const [payload, setPayload] = useState<StrategyReviewPayload | null>(null)
  const [missing, setMissing] = useState(false)
  const [error, setError] = useState('')
  const [building, setBuilding] = useState(false)

  const load = useCallback(async () => {
    if (!reviewable) return
    try {
      const result = await api.strategyReview(workflowId)
      setPayload(result)
      setMissing(result === null)
      setError('')
    } catch {
      setError('原因分析加载失败，稍后自动重试')
    }
  }, [workflowId, reviewable])

  useEffect(() => { void load() }, [load, updatedAt])
  if (!reviewable) return null

  const review = payload?.review
  const evidence = review?.evidence
  const findings = review?.per_channel_findings || []
  const diffs = review?.revision_diff || []
  const downweights = review?.channel_downweights || []
  const evaluationReview = review?.evaluation_review || null
  const notes = review?.notes || []
  const headSummary = error && !review ? '原因分析加载失败' : review ? review.verdict_label : missing ? '这轮还没分析没成的原因' : '正在分析…'

  const rebuild = async () => {
    setBuilding(true)
    setError('')
    try {
      await api.rebuildStrategyReview(workflowId)
      await load()
    } catch (cause) {
      setError(humanizeActionError(cause, '分析失败，请重试。'))
    } finally {
      setBuilding(false)
    }
  }

  return <section className="workflow-insight workflow-review" aria-label="没成的原因">
    <header>
      <span className="insight-icon"><ClipboardCheck /></span>
      <div>
        <span>没成的原因</span>
        <b>{headSummary}</b>
        {review && <small>{`v${review.version || 1} · 生成于 ${review.generated_at || '未知时间'}`}</small>}
      </div>
      {review?.degraded && <span className="tag warn">证据不完整，结论仅供参考</span>}
      {error && <span className="tag warn">{error}</span>}
    </header>
    {missing && !error && <div className="insight-empty">
      <span>这轮还没分析没成的原因。可以基于本轮各渠道的结果和评估情况补一份。</span>
      <button className="button" disabled={building} onClick={() => void rebuild()}>
        {building ? <LoaderCircle className="spin" /> : <RefreshCw />}分析没成的原因
      </button>
    </div>}
    {!review && !missing && !error && <div className="insight-empty"><LoaderCircle className="spin" />正在分析…</div>}
    {review && <>
      <div className="review-verdict">
        <p>{review.verdict_reason}</p>
        {evidence && <div className="funnel-line">
          <span>召回 <b>{evidence.recall_total ?? 0}</b>{evidence.expected_recall_total ? `（预期 ${evidence.expected_recall_total}）` : ''}</span><i>→</i>
          <span>入库新增 <b>{evidence.intake_new_total ?? 0}</b></span><i>→</i>
          <span>评估 <b>{evidence.assessed_total ?? 0}</b></span><i>→</i>
          <span>高分 <b>{evidence.high_score_total ?? 0}</b>{typeof evidence.high_score_rate === 'number' ? `（${Math.round(evidence.high_score_rate * 100)}%）` : ''}</span>
        </div>}
      </div>
      {findings.length > 0 && <div className="review-channels">
        {findings.map(finding => <ChannelFinding key={finding.channel} finding={finding} />)}
      </div>}
      {diffs.length > 0 && <div className="review-diffs">
        <div className="review-diffs-head"><b>修订建议</b><span>在 Agent 中讨论并确认应用</span></div>
        {diffs.map(diff => <DiffRow key={diff.diff_id} diff={diff} />)}
      </div>}
      <StrategyReviewExpansion workflowId={workflowId} signals={review.signals} tree={review.expansion_decision_tree} jobId={jobId} mappingArtifactId={mappingArtifactId} onOpenMapping={onOpenMapping} />
      {downweights.length > 0 && <div className="review-diffs" aria-label="渠道降权建议">
        <div className="review-diffs-head"><b>渠道降权建议</b><span>连续 0 召回（非渠道故障）≥2 轮 · 仅建议不执行，配额调整待顾问确认</span></div>
        {downweights.map((item, index) => <DownweightRow key={`${item.channel}-${index}`} item={item} />)}
      </div>}
      {evaluationReview && <div className="review-diffs review-eval" aria-label="评估尺度复核">
        <div className="review-diffs-head">
          <b>评估尺度复核</b>
          <span>评估 {evaluationReview.assessed_total ?? 0} 人 · 高分 0 · {evaluationReview.prompt || '是尺严还是人不行'}</span>
        </div>
        {(evaluationReview.items || []).length === 0 && <div className="review-diff"><small>未取到被否人选评分证据链，请在候选人列表人工抽查。</small></div>}
        {(evaluationReview.items || []).map(item => <EvaluationItemRow key={item.job_candidate_id || item.assessment_id || item.candidate} item={item} openCandidate={openCandidate} />)}
        {evaluationReview.note && <small className="review-eval-note">{evaluationReview.note}</small>}
      </div>}
      {review.escalation && <p className="review-escalation">转评估问题单：{review.escalation.reason || '评分口径待复核'}</p>}
      {notes.length > 0 && <ul className="review-notes">{notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
    </>}
  </section>
}

function ChannelFinding({ finding }: { finding: StrategyReviewChannelFinding }) {
  const findingLabel = finding.finding && finding.finding !== 'ok' ? FINDING_LABELS[finding.finding] || finding.finding : ''
  return <div className="review-channel">
    <b>{channelLabel(finding.channel)}</b>
    <span className="review-channel-stats">
      召回 {finding.recall_count ?? 0} · 入库 {finding.intake_new_count ?? 0} · 评估 {finding.assessed_count ?? 0} · 高分 {finding.high_score_count ?? 0}
    </span>
    {findingLabel && <span className="tag warn">{findingLabel}</span>}
    {finding.note && <small>{finding.note}</small>}
  </div>
}

function DiffRow({ diff }: { diff: StrategyReviewDiff }) {
  const status = diff.status || 'pending'
  const content = diffContentText(diff)
  return <div className="review-diff">
    <div className="review-diff-head">
      <b>{diffStepLabel(diff.step)} · {diffOpLabel(diff.op)}</b>
      {status === 'accepted' && <span className="tag ok">已采纳</span>}
      {status === 'rejected' && <span className="tag muted">已拒绝</span>}
      {status !== 'accepted' && status !== 'rejected' && <span className="tag">待决策</span>}
    </div>
    {content && <span className="review-diff-content">{content}</span>}
    <small>{diff.reason}</small>
  </div>
}

// S4-5（N4）降权建议行：连续 0 召回（非渠道故障）的 渠道×原型，建议降权并把配额让给高效渠道。
function DownweightRow({ item }: { item: ChannelDownweight }) {
  return <div className="review-diff">
    <div className="review-diff-head">
      <b>{channelLabel(item.channel)}{item.archetype_id ? ` × ${item.archetype_id}` : ''}</b>
      <span className="tag warn">连续 {item.streak ?? 0} 轮 0 召回</span>
    </div>
    {item.reason && <span className="review-diff-content">{item.reason}</span>}
    {item.recommendation && <small>{item.recommendation}</small>}
  </div>
}

// S4-5（N5）被否人选证据链行：遮罩名/当前公司职位/fit_score/关键扣分证据（硬伤在前）。
// "尺度复核"按钮跳候选人详情页，顾问结论经详情页"评分复核"快捷记录写回（既有 commit 链路）。
function EvaluationItemRow({ item, openCandidate }: { item: EvaluationReviewItem; openCandidate?: (id: number) => void }) {
  const deductions = item.deductions || []
  return <div className="review-diff">
    <div className="review-diff-head">
      <b>{item.candidate || '（姓名待补充）'}</b>
      <span className="review-channel-stats">{[item.company, item.title].filter(Boolean).join(' · ') || '公司职位待补充'}</span>
      <span className="tag">fit {item.fit_score ?? '-'}{item.fit_level ? ` · ${item.fit_level}` : ''}</span>
      {openCandidate && item.job_candidate_id != null && <button
        className="button review-eval-open"
        title="打开候选人详情，用「评分复核」记录结论"
        onClick={() => openCandidate(Number(item.job_candidate_id))}
      ><UserRoundSearch />尺度复核</button>}
    </div>
    {deductions.length > 0
      ? <ul className="review-tree-params">{deductions.map((ded, index) => <li key={index}>
          {ded.critical ? '硬伤' : '扣分'}：{ded.criterion || '（未命名准则）'}
          {ded.reason ? `（${ded.reason}）` : ''}
          {ded.evidence && ded.evidence.length > 0 ? `——${ded.evidence.join('；')}` : ''}
        </li>)}</ul>
      : <small>无扣分证据明细，建议打开详情核对评分依据。</small>}
  </div>
}
