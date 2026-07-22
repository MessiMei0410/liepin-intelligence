import type { StrategyReviewDiff } from '../api'

// S4-3 策略复盘 diff 的顾问逐项决策：本期后端无条目级 status 回写接口，
// 决策暂存 localStorage（键含 workflow_id，条目按 diff_id 索引），并在 revise 提交时
// 以"【逐项采纳】…【逐项拒绝】…"后缀并入 instruction，作为 explicit_corrections
// 学习信号的文本载体流入既有审批链。后端补上回写接口后只需替换 load/save 实现。

export type DiffDecision = 'accepted' | 'rejected'

export const DIFF_DECISIONS_EVENT = 'asa:strategy-review-diff-decisions'

const storageKey = (workflowId: string) => `asa_strategy_review_diffs:${workflowId}`

export function loadDiffDecisions(workflowId: string): Record<string, DiffDecision> {
  try {
    const raw = window.localStorage.getItem(storageKey(workflowId))
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const result: Record<string, DiffDecision> = {}
    for (const [diffId, decision] of Object.entries(parsed as Record<string, unknown>)) {
      if (decision === 'accepted' || decision === 'rejected') result[diffId] = decision
    }
    return result
  } catch {
    return {}
  }
}

// decision 传 null 表示撤销（回到 pending）。保存后广播事件，复盘展示区据此刷新决策标记。
export function saveDiffDecision(workflowId: string, diffId: string, decision: DiffDecision | null): void {
  try {
    const decisions = loadDiffDecisions(workflowId)
    if (decision === null) delete decisions[diffId]
    else decisions[diffId] = decision
    window.localStorage.setItem(storageKey(workflowId), JSON.stringify(decisions))
  } catch {
    // localStorage 不可用（隐私模式等）：决策仅保留在本次会话内存中，不阻断交互。
  }
  window.dispatchEvent(new Event(DIFF_DECISIONS_EVENT))
}

const STEP_LABELS: Record<string, string> = {
  step1_job_essence: '岗位本质',
  step2_target_pool: '目标公司池',
  step3_level_mapping: '定档口径',
  step4_keyword_groups: '关键词组',
  step5_expectation: '召回预期',
}

export const diffStepLabel = (step: string): string => STEP_LABELS[step] || step

const OP_LABELS: Record<StrategyReviewDiff['op'], string> = {
  add: '增列',
  replace: '替换',
  review: '复核',
}

export const diffOpLabel = (op: StrategyReviewDiff['op']): string => OP_LABELS[op] || op

// diff 的建议内容摘要：step2 增列为 "T2：甲、乙"，step4 替换为 "「组名」词1、词2"，review 无附加内容。
export function diffContentText(diff: StrategyReviewDiff): string {
  if (diff.companies && diff.companies.length > 0) {
    return `${diff.tier ? `${diff.tier}：` : ''}${diff.companies.join('、')}`
  }
  if (diff.terms && diff.terms.length > 0) {
    return `${diff.group ? `「${diff.group}」` : ''}${diff.terms.join('、')}`
  }
  return ''
}

// 采纳后预填进修改意见 textarea 的一行（顾问可继续编辑再提交）。
export function diffSuggestionText(diff: StrategyReviewDiff): string {
  const content = diffContentText(diff)
  return `${diffOpLabel(diff.op)}${diffStepLabel(diff.step)}${content ? `：${content}` : ''}`
}

// 并入 revise instruction 尾部的逐项决策清单，格式：
// "【逐项采纳】diff-1；diff-2 【逐项拒绝】diff-3"（空前缀换行；无决策时返回空串）。
// 顺序按 revision_diff 原始条目序，保证文本稳定可解析。
export function buildDecisionSuffix(diffs: StrategyReviewDiff[], decisions: Record<string, DiffDecision>): string {
  const accepted = diffs.filter(diff => decisions[diff.diff_id] === 'accepted').map(diff => diff.diff_id)
  const rejected = diffs.filter(diff => decisions[diff.diff_id] === 'rejected').map(diff => diff.diff_id)
  const parts: string[] = []
  if (accepted.length > 0) parts.push(`【逐项采纳】${accepted.join('；')}`)
  if (rejected.length > 0) parts.push(`【逐项拒绝】${rejected.join('；')}`)
  return parts.length > 0 ? `\n${parts.join(' ')}` : ''
}
