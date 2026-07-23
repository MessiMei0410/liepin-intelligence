import type { ExpansionKeywordGroup, ExpansionTreeStep } from '../api'
import { channelLabel } from './utils'
import { DIFF_DECISIONS_EVENT } from './strategyReviewDiff'
import type { DiffDecision } from './strategyReviewDiff'

// S4-3c-3（N3）扩池决策树的顾问逐项决策与渲染辅助。
// 后端本期无树 status 回写接口（PATCH /strategy-review/diffs 仅收 revision_diff 条目），
// 决策只走 localStorage（键含 workflow_id，条目按 step_id 索引）；revise 提交时以
// "【采纳步骤】…【拒绝步骤】…" 后缀并入 instruction（与 diff 的逐项后缀并存，保留作审计痕）。
// 决策变更广播与 diff 共用同一事件，复盘卡据此刷新决策标记。
// 建议后端后续为 expansion_decision_tree[].status 补回写路由（同 diffs 的 PATCH 模式），届时切换持久化。

const storageKey = (workflowId: string) => `asa_strategy_expansion_tree:${workflowId}`

export function loadTreeDecisions(workflowId: string): Record<string, DiffDecision> {
  try {
    const raw = window.localStorage.getItem(storageKey(workflowId))
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const result: Record<string, DiffDecision> = {}
    for (const [stepId, decision] of Object.entries(parsed as Record<string, unknown>)) {
      if (decision === 'accepted' || decision === 'rejected') result[stepId] = decision
    }
    return result
  } catch {
    return {}
  }
}

// decision 传 null 表示撤销（回到待决策）。仅写本地缓存并广播事件，不发请求（后端本期无树回写接口）。
export function saveTreeDecision(workflowId: string, stepId: string, decision: DiffDecision | null): void {
  const decisions = loadTreeDecisions(workflowId)
  if (decision === null) delete decisions[stepId]
  else decisions[stepId] = decision
  try {
    window.localStorage.setItem(storageKey(workflowId), JSON.stringify(decisions))
  } catch {
    // localStorage 不可用（隐私模式等）：决策仅保留在本次会话内存中，不阻断交互。
  }
  window.dispatchEvent(new Event(DIFF_DECISIONS_EVENT))
}

const ACTION_LABELS: Record<string, string> = {
  swap_keywords: '换关键词组',
  expand_pool: '扩池',
  relax_condition: '放宽条件',
  rebalance_channel: '渠道再平衡',
  escalate_mapping: '转 Mapping 校准',
}

export const expansionActionLabel = (actionType: string): string => ACTION_LABELS[actionType] || actionType

const ESCALATE_ACTION_LABELS: Record<string, string> = {
  mapping_direct_sourcing: 'Mapping 直挖',
  client_direction_calibration: '与客户校准方向',
}

// params 缺省占位：后端只取真实值、取不到留空，前端如实显示待补充，不编造。
export const TREE_PARAM_MISSING = '待顾问补充'

// 决策树按 order 升序（order 即执行优先级）；order 缺失的排最后，同序保持原数组顺序（sort 稳定）。
export const sortedTreeSteps = (tree: ExpansionTreeStep[]): ExpansionTreeStep[] =>
  [...tree].sort((a, b) => (a.order ?? Number.MAX_SAFE_INTEGER) - (b.order ?? Number.MAX_SAFE_INTEGER))

const keywordGroupText = (group: ExpansionKeywordGroup): string =>
  `${group.group ? `「${group.group}」` : ''}${(group.terms || []).join('、')}`

// 每步 params 摘要行（复盘卡与采纳预填共用）：公司列表/关键词组/放宽项逐项展开，缺省项如实显示"待顾问补充"。
export function treeStepSummary(step: ExpansionTreeStep): string[] {
  const params = step.params || {}
  switch (step.action_type) {
    case 'swap_keywords': {
      const current = (params.current_groups || []).map(keywordGroupText).filter(Boolean)
      const candidate = (params.candidate_groups || []).map(keywordGroupText).filter(Boolean)
      return [
        `当前词组：${current.join('；') || TREE_PARAM_MISSING}`,
        `候选词组：${candidate.join('；') || TREE_PARAM_MISSING}`,
      ]
    }
    case 'expand_pool': {
      if (!params.next_tier) return ['T1/T2/T3 均已入池，待知识资产更新或顾问指定新池']
      const lines = [`扩向 ${params.tier_label || params.next_tier}：${(params.companies || []).join('、') || TREE_PARAM_MISSING}`]
      if (params.rationale) lines.push(`依据：${params.rationale}`)
      return lines
    }
    case 'relax_condition': {
      const items = params.items || []
      if (items.length === 0) return [TREE_PARAM_MISSING]
      return items.map(item => {
        const current = Array.isArray(item.current) ? item.current.join('、') : item.current
        const parts = [`${item.field || '条件'}：${current || TREE_PARAM_MISSING} → ${item.proposal || TREE_PARAM_MISSING}`]
        if (item.cost) parts.push(`代价：${item.cost}`)
        if (item.note) parts.push(item.note)
        return parts.join('；')
      })
    }
    case 'rebalance_channel': {
      const stats = (params.channel_stats || []).map(stat => {
        const conversion = typeof stat.intake_conversion === 'number' ? `（${Math.round(stat.intake_conversion * 100)}%）` : ''
        return `${channelLabel(stat.channel || '')} 入库/去重 ${stat.intake_new_count ?? 0}/${stat.unique_count ?? 0}${conversion}`
      })
      return [
        `渠道转化：${stats.join('；') || TREE_PARAM_MISSING}`,
        params.recommended_channel ? `建议倾斜：${channelLabel(params.recommended_channel)}` : '暂无可倾斜渠道',
      ]
    }
    case 'escalate_mapping': {
      const actions = (params.actions || []).map(action => ESCALATE_ACTION_LABELS[action] || action)
      return [`升级路径：${actions.join('、') || TREE_PARAM_MISSING}`]
    }
    default:
      return []
  }
}

// 采纳后预填进修改意见 textarea 的文本块（顾问可继续编辑再提交）：首行步骤标题，随后 params 摘要行。
export function treeSuggestionText(step: ExpansionTreeStep): string {
  return [step.title || expansionActionLabel(step.action_type), ...treeStepSummary(step)].join('\n')
}

// 并入 revise instruction 尾部的树决策清单，格式：
// "【采纳步骤】exp-2；exp-3 【拒绝步骤】exp-1"（空前缀换行；无决策时返回空串）。
// 顺序按决策树原始条目序，保证文本稳定可解析。
export function buildTreeDecisionSuffix(tree: ExpansionTreeStep[], decisions: Record<string, DiffDecision>): string {
  const accepted = tree.filter(step => decisions[step.step_id] === 'accepted').map(step => step.step_id)
  const rejected = tree.filter(step => decisions[step.step_id] === 'rejected').map(step => step.step_id)
  const parts: string[] = []
  if (accepted.length > 0) parts.push(`【采纳步骤】${accepted.join('；')}`)
  if (rejected.length > 0) parts.push(`【拒绝步骤】${rejected.join('；')}`)
  return parts.length > 0 ? `\n${parts.join(' ')}` : ''
}
