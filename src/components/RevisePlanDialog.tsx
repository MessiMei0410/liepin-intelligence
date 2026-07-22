import { useEffect, useState } from 'react'
import { SquarePen, X } from 'lucide-react'
import { api } from '../api'
import type { StrategyReviewDiff } from '../api'
import { buildDecisionSuffix, diffContentText, diffOpLabel, diffStepLabel, diffSuggestionText, loadDiffDecisions, saveDiffDecision } from '../workflows/strategyReviewDiff'
import type { DiffDecision } from '../workflows/strategyReviewDiff'

// 工作流“修改计划”内联对话框：替代原 window.prompt，样式复用候选人操作确认层（.action-dialog）。
// 空输入禁止提交；Esc 或点击遮罩取消。提交时回调已 trim 的修改意见并交由调用方发 action('revise')。
// S4-3：传入 workflowId 时拉取策略复盘的 revision_diff 展示在对话框上部，顾问逐项采纳/拒绝——
// 采纳把建议内容预填进 textarea（可继续编辑再提交）；决策暂存 localStorage（后端本期无条目级回写
// 接口），提交时把逐项清单并入 instruction 尾部（"【逐项采纳】…【逐项拒绝】…"），作为
// explicit_corrections 学习信号的文本载体走既有 revise 审批链。复盘拉取失败不影响原有修改流程。
export function RevisePlanDialog({ workflowId, onCancel, onSubmit }: { workflowId?: string; onCancel: () => void; onSubmit: (instruction: string) => void }) {
  const [instruction, setInstruction] = useState('')
  const [diffs, setDiffs] = useState<StrategyReviewDiff[]>([])
  const [decisions, setDecisions] = useState<Record<string, DiffDecision>>({})
  const valid = instruction.trim().length > 0

  useEffect(() => {
    if (!workflowId) return
    let alive = true
    api.strategyReview(workflowId)
      .then(payload => {
        if (!alive || !payload) return
        // Core 返回动态 dict：review 缺失时按无 diff 处理，不影响原有修改流程。
        setDiffs(payload.review?.revision_diff || [])
        setDecisions(loadDiffDecisions(workflowId))
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

  const submit = () => {
    if (!valid) return
    onSubmit(instruction.trim() + buildDecisionSuffix(diffs, decisions))
  }

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
            <div className="revise-diffs" aria-label="策略复盘修订建议">
              <div className="revise-diffs-head"><b>策略复盘修订建议</b><span>采纳将预填进修改意见，决策随提交进入学习信号</span></div>
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
