import { useEffect, useState } from 'react'
import { SquarePen, X } from 'lucide-react'
import { api } from '../api'
import type { ExpansionTreeStep, StrategyReviewDiff } from '../api'
import { buildDecisionSuffix, diffContentText, diffOpLabel, diffStepLabel, diffSuggestionText, mergeReviewDecisions, loadDiffDecisions, persistDiffDecisions, saveDiffDecision } from '../workflows/strategyReviewDiff'
import type { DiffDecision } from '../workflows/strategyReviewDiff'
import { buildTreeDecisionSuffix, expansionActionLabel, loadTreeDecisions, saveTreeDecision, sortedTreeSteps, treeStepSummary, treeSuggestionText } from '../workflows/strategyExpansionTree'

// 工作流“修改计划”内联对话框：替代原 window.prompt，样式复用候选人操作确认层（.action-dialog）。
// 空输入禁止提交；Esc 或点击遮罩取消。提交时回调已 trim 的修改意见并交由调用方发 action('revise')。
// S4-3：传入 workflowId 时拉取策略复盘的 revision_diff 展示在对话框上部，顾问逐项采纳/拒绝——
// 采纳把建议内容预填进 textarea（可继续编辑再提交）。S4-3c 起决策经 PATCH /strategy-review/diffs
// 持久化到后端（事实源为复盘的 revision_diff[].status，localStorage 仅作 API 失败时的缓存回退）；
// 提交时把逐项清单并入 instruction 尾部（"【逐项采纳】…【逐项拒绝】…"，保留作审计痕），并同步发
// 一次 PATCH（与 revise 各自幂等）。复盘拉取失败不影响原有修改流程。
// S4-3c-3：diff 区下方渲染扩池决策树（expansion_decision_tree）为可选步骤，同样逐项采纳/拒绝——
// 树本期无后端 status 回写接口，决策只走 localStorage（键含 workflow_id+step_id 维度），采纳把该步
// params 摘要预填进 textarea，提交时以 "【采纳步骤】…【拒绝步骤】…" 后缀并入 instruction。
export function RevisePlanDialog({ workflowId, onCancel, onSubmit }: { workflowId?: string; onCancel: () => void; onSubmit: (instruction: string) => void }) {
  const [instruction, setInstruction] = useState('')
  const [diffs, setDiffs] = useState<StrategyReviewDiff[]>([])
  const [decisions, setDecisions] = useState<Record<string, DiffDecision>>({})
  const [tree, setTree] = useState<ExpansionTreeStep[]>([])
  const [treeDecisions, setTreeDecisions] = useState<Record<string, DiffDecision>>({})
  const valid = instruction.trim().length > 0

  useEffect(() => {
    if (!workflowId) return
    let alive = true
    api.strategyReview(workflowId)
      .then(payload => {
        if (!alive || !payload) return
        // Core 返回动态 dict：review 缺失时按无 diff 处理，不影响原有修改流程。
        // 决策以复盘 revision_diff[].status 为事实源合并本地缓存（API 失败时 load 降级回缓存）。
        setDiffs(payload.review?.revision_diff || [])
        setDecisions(mergeReviewDecisions(workflowId, payload.review?.revision_diff))
        // S4-3c-3 扩池决策树：无后端回写接口，决策仅以本地缓存为事实源。
        setTree(payload.review?.expansion_decision_tree || [])
        setTreeDecisions(loadTreeDecisions(workflowId))
      })
      .catch(() => { /* 复盘不可达不阻断修改计划 */ })
    return () => { alive = false }
  }, [workflowId])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onCancel() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  const decide = (diff: StrategyReviewDiff, decision: DiffDecision) => {
    if (!workflowId) return
    // 重复点击同一决策视为撤销（回到待决策）。
    const next: DiffDecision | null = decisions[diff.diff_id] === decision ? null : decision
    saveDiffDecision(workflowId, diff.diff_id, next)
    setDecisions(loadDiffDecisions(workflowId))
    if (next === 'accepted') {
      // 采纳：建议内容预填进修改意见（顾问可改再提交）；已含该行则不重复追加。
      const suggestion = diffSuggestionText(diff)
      setInstruction(prev => (prev.split('\n').includes(suggestion) ? prev : prev ? `${prev}\n${suggestion}` : suggestion))
    }
  }

  // 决策树逐项采纳/拒绝：与 diff 同法（重复点击同一决策视为撤销），但仅落 localStorage——
  // 后端本期无树 status 回写接口，不发 PATCH。
  const decideTree = (step: ExpansionTreeStep, decision: DiffDecision) => {
    if (!workflowId) return
    const next: DiffDecision | null = treeDecisions[step.step_id] === decision ? null : decision
    saveTreeDecision(workflowId, step.step_id, next)
    setTreeDecisions(loadTreeDecisions(workflowId))
    if (next === 'accepted') {
      // 采纳：该步 params 摘要预填进修改意见（顾问可改再提交）；已含该块则不重复追加。
      const suggestion = treeSuggestionText(step)
      setInstruction(prev => (prev.includes(suggestion) ? prev : prev ? `${prev}\n${suggestion}` : suggestion))
    }
  }

  const submit = () => {
    if (!valid) return
    // 决策随提交整体回写后端（fire-and-forget，与 revise 各自幂等）；instruction 后缀保留作审计痕。
    if (workflowId) persistDiffDecisions(workflowId, diffs, decisions)
    onSubmit(instruction.trim() + buildDecisionSuffix(diffs, decisions) + buildTreeDecisionSuffix(tree, treeDecisions))
  }

  const treeSteps = sortedTreeSteps(tree)

  return (
    <div className="action-dialog-backdrop" role="presentation" onClick={onCancel}>
      <section className="action-dialog" role="dialog" aria-modal="true" aria-labelledby="revise-plan-title" onClick={event => event.stopPropagation()}>
        <header>
          <span className="action-dialog-icon"><SquarePen /></span>
          <div><small>工作流</small><h3 id="revise-plan-title">修改计划</h3></div>
          <button className="icon-btn" onClick={onCancel} title="取消" aria-label="取消"><X /></button>
        </header>
        <div className="action-dialog-body">
          {diffs.length > 0 && (
            <div className="revise-diffs" aria-label="修订建议">
              <div className="revise-diffs-head"><b>修订建议</b><span>采纳将预填进修改意见，决策随提交进入学习信号</span></div>
              {diffs.map(diff => {
                const decision = decisions[diff.diff_id]
                const content = diffContentText(diff)
                return (
                  <div className="revise-diff-item" key={diff.diff_id}>
                    <div className="revise-diff-line">
                      <b>{diffStepLabel(diff.step)} · {diffOpLabel(diff.op)}</b>
                      {content && <span>{content}</span>}
                    </div>
                    <p>{diff.reason}</p>
                    <div className="revise-diff-actions">
                      <button
                        type="button"
                        className={`button ${decision === 'accepted' ? 'primary' : ''}`}
                        aria-pressed={decision === 'accepted'}
                        onClick={() => decide(diff, 'accepted')}
                      >采纳</button>
                      <button
                        type="button"
                        className={`button ${decision === 'rejected' ? 'danger' : ''}`}
                        aria-pressed={decision === 'rejected'}
                        onClick={() => decide(diff, 'rejected')}
                      >拒绝</button>
                      {decision && <span className={`tag ${decision === 'accepted' ? 'ok' : 'muted'}`}>{decision === 'accepted' ? '已采纳' : '已拒绝'}</span>}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          {treeSteps.length > 0 && (
            <div className="revise-diffs revise-tree" aria-label="人不够时的扩圈建议">
              <div className="revise-diffs-head"><b>人不够时的扩圈建议</b><span>采纳将预填进修改意见，决策随提交并入指令</span></div>
              {treeSteps.map(step => {
                const decision = treeDecisions[step.step_id]
                const summary = treeStepSummary(step)
                return (
                  <div className="revise-diff-item" key={step.step_id}>
                    <div className="revise-diff-line">
                      <b>{typeof step.order === 'number' ? `${step.order}. ` : ''}{expansionActionLabel(step.action_type)}</b>
                      {step.title && <span>{step.title}</span>}
                    </div>
                    {summary.length > 0 && <ul className="revise-tree-params">{summary.map((line, index) => <li key={index}>{line}</li>)}</ul>}
                    {step.detail && <p>{step.detail}</p>}
                    <div className="revise-diff-actions">
                      <button
                        type="button"
                        className={`button ${decision === 'accepted' ? 'primary' : ''}`}
                        aria-pressed={decision === 'accepted'}
                        onClick={() => decideTree(step, 'accepted')}
                      >采纳</button>
                      <button
                        type="button"
                        className={`button ${decision === 'rejected' ? 'danger' : ''}`}
                        aria-pressed={decision === 'rejected'}
                        onClick={() => decideTree(step, 'rejected')}
                      >拒绝</button>
                      {decision && <span className={`tag ${decision === 'accepted' ? 'ok' : 'muted'}`}>{decision === 'accepted' ? '已采纳' : '已拒绝'}</span>}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          <label>
            <span>修改意见（必填）</span>
            <textarea value={instruction} onChange={event => setInstruction(event.target.value)} placeholder="例如：优先补充华东区域的候选人，提高学历门槛" rows={4} autoFocus />
          </label>
        </div>
        <footer>
          <button className="button" onClick={onCancel}>取消</button>
          <button className="button primary" disabled={!valid} onClick={submit}>确认修改</button>
        </footer>
      </section>
    </div>
  )
}
