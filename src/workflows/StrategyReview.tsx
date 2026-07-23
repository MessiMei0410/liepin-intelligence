import { useCallback, useEffect, useReducer, useState } from 'react'
import { ClipboardCheck, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '../api'
import type { StrategyReviewChannelFinding, StrategyReviewDiff, StrategyReviewPayload } from '../api'
import { humanizeActionError } from '../shared/errors'
import { channelLabel } from './utils'
import { DIFF_DECISIONS_EVENT, diffContentText, diffOpLabel, diffStepLabel, loadDiffDecisions, mergeReviewDecisions } from './strategyReviewDiff'
import { StrategyReviewExpansion } from './StrategyReviewExpansion'

// S4-3 策略复盘：终局工作流（completed/blocked/failed）展示规则版复盘结论与修订建议 diff。
// 独立按需路由 /strategy-review，面板挂载与详情刷新（updatedAt 变化）时拉取，不进 /summary 轮询签名。
// 无复盘（404）显示"该轮未生成策略复盘"并提供"生成复盘"（调 rebuild 幂等重算）。
// degraded / insufficient_data 按后端语义如实呈现（证据不完整仅供参考），不夸大结论。
// 逐项采纳/拒绝在"调整条件再搜"对话框（RevisePlanDialog）内操作，此处只回显决策标记；
// S4-3c 起决策以后端 revision_diff[].status 为事实源（每次拉取后合并进本地缓存），
// localStorage 仅作 API 失败时的缓存回退，变更事件到达即刷新。
// S4-3c-3：池枯竭信号与扩池决策树扩区在 StrategyReviewExpansion（树决策本期仅 localStorage，无后端回写）。

const REVIEWABLE_STATUSES = new Set(['completed', 'blocked', 'failed'])

const FINDING_LABELS: Record<string, string> = {
  execution_issue: '执行/渠道',
  zero_recall: '零召回',
  low_high_rate: '高分率低',
}

export function StrategyReview({ workflowId, status, updatedAt }: { workflowId: string; status: string; updatedAt: string }) {
  const reviewable = REVIEWABLE_STATUSES.has(status)
  const [payload, setPayload] = useState<StrategyReviewPayload | null>(null)
  const [missing, setMissing] = useState(false)
  const [error, setError] = useState('')
  const [building, setBuilding] = useState(false)
  const [, onDecisionsChanged] = useReducer((tick: number) => tick + 1, 0)

  const load = useCallback(async () => {
    if (!reviewable) return
    try {
      const result = await api.strategyReview(workflowId)
      // 后端已决状态为事实源：合并进本地缓存（未决条目的本地暂存保留），展示以合并结果为准。
      mergeReviewDecisions(workflowId, result?.review?.revision_diff)
      setPayload(result)
      setMissing(result === null)
      setError('')
    } catch {
      setError('复盘加载失败，稍后自动重试')
    }
  }, [workflowId, reviewable])

  useEffect(() => { void load() }, [load, updatedAt])
  useEffect(() => {
    window.addEventListener(DIFF_DECISIONS_EVENT, onDecisionsChanged)
    return () => window.removeEventListener(DIFF_DECISIONS_EVENT, onDecisionsChanged)
  }, [])

  if (!reviewable) return null

  const review = payload?.review
  const evidence = review?.evidence
  const findings = review?.per_channel_findings || []
  const diffs = review?.revision_diff || []
  const notes = review?.notes || []
  const decisions = loadDiffDecisions(workflowId)
  const headSummary = error && !review ? '复盘加载失败' : review ? review.verdict_label : missing ? '该轮未生成策略复盘' : '复盘加载中…'

  const rebuild = async () => {
    setBuilding(true)
    setError('')
    try {
      await api.rebuildStrategyReview(workflowId)
      await load()
    } catch (cause) {
      setError(humanizeActionError(cause, '生成复盘失败，请重试。'))
    } finally {
      setBuilding(false)
    }
  }

  return <section className="workflow-insight workflow-review" aria-label="策略复盘">
    <header>
      <span className="insight-icon"><ClipboardCheck /></span>
      <div>
        <span>策略复盘</span>
        <b>{headSummary}</b>
        {review && <small>{`v${review.version || 1} · 生成于 ${review.generated_at || '未知时间'}`}</small>}
      </div>
      {review?.degraded && <span className="tag warn">证据不完整，结论仅供参考</span>}
      {error && <span className="tag warn">{error}</span>}
    </header>
    {missing && !error && <div className="insight-empty">
      <span>该轮未生成策略复盘。可基于本轮渠道漏斗与评估结果补生成。</span>
      <button className="button" disabled={building} onClick={() => void rebuild()}>
        {building ? <LoaderCircle className="spin" /> : <RefreshCw />}生成复盘
      </button>
    </div>}
    {!review && !missing && !error && <div className="insight-empty"><LoaderCircle className="spin" />复盘加载中…</div>}
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
        <div className="review-diffs-head"><b>修订建议</b><span>逐项采纳/拒绝在“调整条件再搜”中操作</span></div>
        {diffs.map(diff => <DiffRow key={diff.diff_id} diff={diff} decision={decisions[diff.diff_id]} />)}
      </div>}
      <StrategyReviewExpansion workflowId={workflowId} signals={review.signals} tree={review.expansion_decision_tree} />
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

function DiffRow({ diff, decision }: { diff: StrategyReviewDiff; decision?: string }) {
  const status = decision || diff.status || 'pending'
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
