import { useState } from 'react'
import { LoaderCircle, MapPinned } from 'lucide-react'
import { api } from '../api'
import type { ExpansionTreeStep, StrategyReviewSignal } from '../api'
import { humanizeActionError } from '../shared/errors'
import { expansionActionLabel, sortedTreeSteps, treeStepSummary } from './strategyExpansionTree'
import { MAPPING_TRIGGER_BY_TREE } from './mappingTask'

// S4-3c-3（N3）复盘卡扩区：池枯竭信号（tag + detail 一行）与扩池决策树编号步骤列表。
// 策略调整只在 Copilot 确认卡中落地；这里仅展示复盘证据和后端回传状态。
// 无信号且无决策树（旧复盘）时不渲染。
// S5-2：escalate_mapping 步旁挂「发起 Mapping 直挖」入口——已有任务卡（本工作流产物含
// mapping_task）直接打开；否则调 POST /jobs/{job_id}/mapping-tasks（trigger=decision_tree_exhausted）
// 创建后打开。工作流无 job 上下文（jobId 缺失）时按钮不显示。
export function StrategyReviewExpansion({ workflowId, signals, tree, jobId, mappingArtifactId, onOpenMapping, onChanged }: {
  workflowId: string
  signals?: StrategyReviewSignal[]
  tree?: ExpansionTreeStep[]
  jobId?: number
  mappingArtifactId?: string
  onOpenMapping?: (artifactId: string) => void
  onChanged?: () => void | Promise<void>
}) {
  const signalList = signals || []
  const steps = sortedTreeSteps(tree || [])
  if (signalList.length === 0 && steps.length === 0) return null
  return <>
    {signalList.length > 0 && <div className="review-signals">
      {signalList.map((signal, index) => <div className="review-signal" key={signal.signal || index}>
        <span className="tag warn">{signal.label || signal.signal}</span>
        {signal.detail && <span className="review-signal-detail">{signal.detail}</span>}
      </div>)}
    </div>}
    {steps.length > 0 && <div className="review-tree" data-workflow-id={workflowId}>
      <div className="review-diffs-head"><b>人不够时的扩圈建议</b><span>按序评估，在 Agent 中讨论并确认应用</span></div>
      {steps.map(step => {
        const status = step.status || 'pending'
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
          {step.action_type === 'escalate_mapping' && jobId != null && jobId > 0 && onOpenMapping && (
            <MappingEntryButton jobId={jobId} mappingArtifactId={mappingArtifactId || ''} onOpenMapping={onOpenMapping} onChanged={onChanged} />
          )}
        </div>
      })}
    </div>}
  </>
}

// 「发起 Mapping 直挖」入口：已存在任务卡直接打开（不重复发起采集）；否则创建后打开。
// 创建走幂等写（Idempotency-Key + request_id），409（岗位无 strategy_v2 等）中文原因直接透出。
function MappingEntryButton({ jobId, mappingArtifactId, onOpenMapping, onChanged }: {
  jobId: number
  mappingArtifactId: string
  onOpenMapping: (artifactId: string) => void
  onChanged?: () => void | Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const open = async () => {
    if (mappingArtifactId) {
      onOpenMapping(mappingArtifactId)
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await api.createMappingTask(jobId, MAPPING_TRIGGER_BY_TREE)
      onOpenMapping(result.artifact_id)
      if (onChanged) void Promise.resolve().then(onChanged).catch(() => undefined)
    } catch (cause) {
      setError(humanizeActionError(cause, '发起失败，请重试。'))
    } finally {
      setBusy(false)
    }
  }
  return <div className="review-mapping-entry">
    <button className="button" disabled={busy} onClick={() => void open()}>
      {busy ? <LoaderCircle className="spin" /> : <MapPinned />}{mappingArtifactId ? '打开 Mapping 任务卡' : '发起 Mapping 直挖'}
    </button>
    {error && <span className="tag warn">{error}</span>}
  </div>
}
