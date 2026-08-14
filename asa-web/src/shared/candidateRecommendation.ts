export type CandidateRecommendationTone = 'good' | 'warn' | 'bad' | 'neutral'

const labels: Record<string, string> = {
  recommended: '推荐',
  priority_review: '优先复核',
  verify_first: '待补证据',
  not_recommended: '不推荐',
  hold: '暂缓',
  pending_review: '待复核',
  ready_for_review: '待复核',
  needs_review: '待复核',
  contacted: '已联系',
  continue_pending_manual_contact: '待人工跟进',
  backup: '备选',
  shortlist: '入围',
  reject: '不推进',
  rejected: '不推进',
  age_risk_review: '年龄风险待核',
}

const bad = new Set(['not_recommended', 'reject', 'rejected'])
const good = new Set(['recommended'])
const warn = new Set(['priority_review', 'verify_first', 'hold', 'age_risk_review'])

const normalized = (value?: string | null) => String(value || '').trim()

export const candidateRecommendationLabel = (value?: string | null) => {
  const key = normalized(value)
  if (!key) return '待复核'
  if (labels[key]) return labels[key]
  return /[\u3400-\u9fff]/.test(key) ? key : '待确认'
}

export const candidateRecommendationTone = (value?: string | null): CandidateRecommendationTone => {
  const key = normalized(value)
  if (good.has(key)) return 'good'
  if (bad.has(key)) return 'bad'
  if (warn.has(key)) return 'warn'
  return 'neutral'
}
