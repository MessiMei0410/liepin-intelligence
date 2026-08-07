import { CircleCheck, ShieldCheck, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type { CandidateDetail, RecommendationPackageRef } from '../api'
import { date } from '../shared/format'

// 顾问确认推荐（consultant-confirmed recommendation）：
// 在既有「标记已推荐」preflight→commit 语义之外，追加一条顾问确认的推荐决定
// （推荐理由 + 确认时间），沿用 Core 的 preflight -> commit 保护链。
// 本文件负责：
//  1) 展示评估依据与风险提示——只读 CandidateDetail 既有字段，不新增数据依赖；
//  2) 必填推荐理由输入 + 常用理由快捷文案；
//  3) 幂等写决定记录（Idempotency-Key + request_id，与 api.ts write 同语义），
//     错误携带 status 与可读 detail，由调用方如实呈现，绝不把失败当作成功。
// 确认成功后 Core 同步生成版本化推荐包（package 字段）；缺省（旧后端/未生成）时如实不展示。
// 红线：不 bypass 已停止/已推荐保护（入口由 CandidatePanel 控制）、不新增全局样式、
// 不引入 prompt/confirm/alert、不使用显式 any。

export type RecommendationDecisionRecord = { reason: string; decided_at: string }

export type RecommendationDecisionResult = { decided_at?: string; already_applied?: boolean; reason?: string; package?: RecommendationPackageRef | null }

// 推荐包回执文案：generated=已生成；pending=生成中；缺省=不回执（不冒充成功）。
export const recommendationPackageNote = (pkg?: RecommendationPackageRef | null): string => {
  if (!pkg) return ''
  if (pkg.status === 'generated') return `推荐包 v${pkg.version} 已生成。`
  if (pkg.status === 'pending') return `推荐包 v${pkg.version} 生成中。`
  return `推荐包 v${pkg.version} 状态：${pkg.status}。`
}

export async function recordRecommendationDecision(candidateId: number, reason: string, preflightToken?: string): Promise<RecommendationDecisionResult> {
  const preflight = preflightToken ? undefined : await api.consultantRecommendationPreflight(candidateId)
  const result = await api.consultantRecommendationCommit(candidateId, reason, preflightToken || preflight?.token || '')
  return {
    decided_at: result.confirmed_at,
    reason: result.reason,
    already_applied: result.already_confirmed === true || result.receipt?.idempotent_replay === true,
    package: result.package ?? null,
  }
}

type SignalKey = 'review_pass_count' | 'contacted_count' | 'recommended_count' | 'stopped_count' | 'client_positive_count' | 'client_rejected_count'

const sumSignal = (candidate: CandidateDetail, key: SignalKey) =>
  (candidate.sourcing_attributions || []).reduce((total, item) => total + Number(item[key] || 0), 0)

export const recommendationSignals = (candidate: CandidateDetail): Array<[string, number]> => {
  const rows: Array<[string, SignalKey]> = [
    ['已通过复核', 'review_pass_count'],
    ['已联系', 'contacted_count'],
    ['已推荐', 'recommended_count'],
    ['停止', 'stopped_count'],
    ['客户正向', 'client_positive_count'],
    ['客户否决', 'client_rejected_count'],
  ]
  const signals: Array<[string, number]> = []
  for (const [label, key] of rows) {
    const count = sumSignal(candidate, key)
    if (count > 0) signals.push([label, count])
  }
  return signals
}

export const recommendationRiskCues = (candidate: CandidateDetail): string[] => {
  const cues: string[] = []
  if (!candidate.experience && !candidate.education) cues.push('经验/学历字段缺失，推荐前请人工核实')
  const attributions = candidate.sourcing_attributions || []
  const rejected = sumSignal(candidate, 'client_rejected_count')
  const stopped = sumSignal(candidate, 'stopped_count')
  const contacted = sumSignal(candidate, 'contacted_count')
  if (rejected > 0) cues.push(`该候选人曾有 ${rejected} 次客户否决记录，请确认否决原因已澄清`)
  if (stopped > 0) cues.push(`该候选人曾有 ${stopped} 次停止推进记录，请核对停止原因`)
  if (attributions.length > 0 && contacted === 0) cues.push('暂无已联系记录，请确认已与候选人沟通意向')
  return cues
}

export function RecommendationDecisionFields({ candidate, reason, onReason }: { candidate: CandidateDetail; reason: string; onReason: (value: string) => void }) {
  const cues = recommendationRiskCues(candidate)
  const signals = recommendationSignals(candidate)
  const reportCount = (candidate.report_artifacts || []).length
  return (
    <>
      <div className="recommendation-evidence-head" style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 12, fontSize: 10.5, color: 'var(--muted)' }}>
        <ShieldCheck size={14} />
        <span>评估依据与风险提示（来自候选人现有信息）</span>
      </div>
      <dl>
        <div><dt>当前阶段</dt><dd>{candidate.clean_stage || candidate.flow_bucket || '待复核'}</dd></div>
        <div><dt>经验 / 学历</dt><dd>{candidate.experience || '-'} · {candidate.education || '-'}</dd></div>
        <div><dt>当前职位</dt><dd>{[candidate.current_title, candidate.current_company].filter(Boolean).join(' @ ') || '-'}</dd></div>
        <div><dt>匹配产物</dt><dd>{reportCount ? `${reportCount} 项匹配/推荐报告` : '暂无匹配/推荐报告'}</dd></div>
      </dl>
      {signals.length > 0 && (
        <div className="learning-signals" style={{ justifyContent: 'flex-start', marginTop: 10 }}>
          {signals.map(([label, count]) => <span key={label}>{label} {count}</span>)}
        </div>
      )}
      {cues.length > 0 && <div className="action-dialog-error"><TriangleAlert /><span>{cues.join('；')}</span></div>}
      <div className="review-conclusion-field">
        <span>常用理由</span>
        <div className="review-conclusion-chips">
          <button type="button" className="button" onClick={() => onReason('硬性要求匹配，候选人意向已确认，建议推进推荐')}>硬性匹配</button>
          <button type="button" className="button" onClick={() => onReason('经验与方向高度吻合，客户画像匹配，建议推荐')}>画像吻合</button>
        </div>
      </div>
      <label>
        <span>推荐理由（必填）</span>
        <textarea value={reason} onChange={(event) => onReason(event.target.value)} placeholder="写清推荐依据：与岗位匹配点、已核实信息、候选人意向…" rows={4} />
      </label>
      <p style={{ marginTop: 10 }}><ShieldCheck />确认后：既有「已推荐」状态照常更新，并记录本理由与确认时间；重复确认不会重复推荐。</p>
    </>
  )
}

export function RecommendationConfirmCard({ record }: { record: RecommendationDecisionRecord }) {
  return (
    <section className="candidate-action-feedback success" role="status" aria-label="推荐确认记录">
      <CircleCheck />
      <span><b>已确认推荐</b>：{record.reason}（确认时间 {date(record.decided_at)}）</span>
    </section>
  )
}
