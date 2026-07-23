import type { ExpansionTreeStep, StrategyReviewSignal } from '../api'
import { expansionActionLabel, loadTreeDecisions, sortedTreeSteps, treeStepSummary } from './strategyExpansionTree'

// S4-3c-3（N3）复盘卡扩区：池枯竭信号（tag + detail 一行）与扩池决策树编号步骤列表。
// 与 revision_diff 同法：此处只读展示并回显决策标记，逐项采纳/拒绝在“调整条件再搜”对话框内操作；
// 树决策本期无后端回写接口，标记事实源为 localStorage（键含 workflow_id，条目按 step_id 索引）。
// 无信号且无决策树（旧复盘）时不渲染。
export function StrategyReviewExpansion({ workflowId, signals, tree }: { workflowId: string; signals?: StrategyReviewSignal[]; tree?: ExpansionTreeStep[] }) {
  const signalList = signals || []
  const steps = sortedTreeSteps(tree || [])
  if (signalList.length === 0 && steps.length === 0) return null
  const decisions = loadTreeDecisions(workflowId)
  return <>
    {signalList.length > 0 && <div className="review-signals">
      {signalList.map((signal, index) => <div className="review-signal" key={signal.signal || index}>
        <span className="tag warn">{signal.label || signal.signal}</span>
        {signal.detail && <span className="review-signal-detail">{signal.detail}</span>}
      </div>)}
    </div>}
    {steps.length > 0 && <div className="review-tree">
      <div className="review-diffs-head"><b>扩池决策树</b><span>按序执行，逐项采纳/拒绝在“调整条件再搜”中操作</span></div>
      {steps.map(step => {
        const status = decisions[step.step_id] || step.status || 'pending'
        const summary = treeStepSummary(step)
        return <div className="review-tree-step" key={step.step_id}>
          <div className="review-tree-head">
            <b>{typeof step.order === 'number' ? `${step.order}. ` : ''}{expansionActionLabel(step.action_type)}</b>
            {step.title && <span className="review-tree-title">{step.title}</span>}
            {status === 'accepted' && <span className="tag ok">已采纳</span>}
            {status === 'rejected' && <span className="tag muted">已拒绝</span>}
            {status !== 'accepted' && status !== 'rejected' && <span className="tag">待决策</span>}
          </div>
          {summary.length > 0 && <ul className="review-tree-params">{summary.map((line, index) => <li key={index}>{line}</li>)}</ul>}
          {step.detail && <small>{step.detail}</small>}
        </div>
      })}
    </div>}
  </>
}
