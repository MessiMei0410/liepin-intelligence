import { api } from '../api'
import type { StrategyReviewDiff } from '../api'

// S4-3 策略复盘 diff 的顾问逐项决策：S4-3c 起持久化到后端（PATCH /strategy-review/diffs，
// 事实源为 GET strategy-review 的 revision_diff[].status）；localStorage 降级为 API 失败时
// 的缓存回退（键含 workflow_id，条目按 diff_id 索引），不阻断交互。revise 提交时仍以
// "【逐项采纳】…【逐项拒绝】…"后缀并入 instruction（保留作审计痕），并同步发一次 PATCH
// （与 revise 各自幂等）。

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

const writeLocalDiffDecisions = (workflowId: string, decisions: Record<string, DiffDecision>): void => {
  try {
    window.localStorage.setItem(storageKey(workflowId), JSON.stringify(decisions))
  } catch {
    // localStorage 不可用（隐私模式等）：决策仅保留在本次会话内存中，不阻断交互。
  }
}

// API 事实源合并：复盘 revision_diff 的已决状态覆盖同名本地暂存（后端为准），
// 本地仅保留后端仍 pending 条目的暂存决策；合并结果写回缓存并返回。
export function mergeReviewDecisions(workflowId: string, diffs: StrategyReviewDiff[] | undefined): Record<string, DiffDecision> {
  const merged = loadDiffDecisions(workflowId)
  let changed = false
  for (const diff of diffs || []) {
    if ((diff.status === 'accepted' || diff.status === 'rejected') && merged[diff.diff_id] !== diff.status) {
      merged[diff.diff_id] = diff.status
      changed = true
    }
  }
  if (changed) writeLocalDiffDecisions(workflowId, merged)
  return merged
}

// decision 传 null 表示撤销（回到 pending）。本地缓存即时更新并广播事件，复盘展示区据此刷新
// 决策标记；已决状态同步 PATCH 落库（fire-and-forget：失败时本地缓存兜底，不阻断交互）。
// 撤销（null）本期只落本地——PATCH 契约仅收 accepted/rejected，后端无回到 pending 的条目级接口。
export function saveDiffDecision(workflowId: string, diffId: string, decision: DiffDecision | null): void {
  const decisions = loadDiffDecisions(workflowId)
  if (decision === null) delete decisions[diffId]
  else decisions[diffId] = decision
  writeLocalDiffDecisions(workflowId, decisions)
  window.dispatchEvent(new Event(DIFF_DECISIONS_EVENT))
  if (decision !== null) {
    void api.patchStrategyReviewDiffs(workflowId, [{ diff_id: diffId, status: decision }]).catch(() => {
      // API 不可达：localStorage 缓存已留底，下次 GET 合并时仍以本地暂存呈现，不阻断交互。
    })
  }
}

// 提交 revise 时的一次性回写：把当前已决集合（按 revision_diff 原始条目序，稳定可解析）
// PATCH 落库。fire-and-forget——本地缓存已兜底，成败均不阻断 revise 主流程。
export function persistDiffDecisions(
  workflowId: string,
  diffs: StrategyReviewDiff[],
  decisions: Record<string, DiffDecision>,
): void {
  const decided = diffs
    .filter(diff => decisions[diff.diff_id] === 'accepted' || decisions[diff.diff_id] === 'rejected')
    .map(diff => ({ diff_id: diff.diff_id, status: decisions[diff.diff_id] as DiffDecision }))
  if (decided.length === 0) return
  void api.patchStrategyReviewDiffs(workflowId, decided).catch(() => {
    // API 不可达：决策已在 localStorage 缓存，下一轮 GET 合并前不丢交互。
  })
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
