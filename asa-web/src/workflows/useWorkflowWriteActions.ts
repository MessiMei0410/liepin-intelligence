import { useState } from 'react'
import { api, type Workflow } from '../api'
import { humanizeActionError } from '../shared/errors'

type WorkflowActionName = 'start' | 'pause' | 'resume' | 'cancel' | 'archive'
// 需要确认卡的工作流写操作（P7）：与候选人操作的预检确认链对齐，不再点击即执行。
export type ConfirmableWorkflowAction = 'pause' | 'resume' | 'cancel' | 'archive'
const CONFIRM_WORKFLOW_ACTIONS: ReadonlySet<WorkflowActionName> = new Set(['pause', 'resume', 'cancel', 'archive'])
type ApprovalDecision = 'approve' | 'reject'

type StoredWorkflow = {
  sourceRevision: string
  value: Workflow
}

const workflowRevision = (value: Workflow) => [
  value.workflow.workflow_id,
  value.workflow.status,
  value.workflow.updated_at || '',
  value.steps.map(step => `${step.id}:${step.status}:${step.updated_at || ''}`).join(','),
  value.approvals.map(approval => `${approval.approval_id}:${approval.status}`).join(','),
].join('|')

const completeWorkflow = (payload: unknown): Workflow | null => {
  if (!payload || typeof payload !== 'object') return null
  const candidate = payload as Partial<Workflow>
  if (
    !candidate.workflow?.workflow_id
    || !candidate.goal
    || !Array.isArray(candidate.steps)
    || !Array.isArray(candidate.approvals)
    || !Array.isArray(candidate.artifacts)
  ) return null
  return candidate as Workflow
}

const fallbackActionValue = (value: Workflow, action: WorkflowActionName): Workflow => {
  const nextStatus = {
    start: 'queued',
    pause: 'paused',
    resume: 'queued',
    cancel: 'cancelled',
    archive: value.workflow.status,
  }[action]
  const steps = value.steps.map(step => {
    if (action === 'pause' && step.status === 'running') return { ...step, status: 'paused' }
    if (action === 'resume' && step.status === 'paused') return { ...step, status: 'queued' }
    if (action === 'cancel' && ['pending', 'queued', 'running', 'waiting_approval', 'waiting_external', 'approved', 'paused'].includes(step.status)) {
      return { ...step, status: 'cancelled' }
    }
    return step
  })
  return {
    ...value,
    goal: { ...value.goal, status: nextStatus },
    workflow: { ...value.workflow, status: nextStatus },
    steps,
    approvals: action === 'cancel'
      ? value.approvals.map(approval => approval.status === 'pending' ? { ...approval, status: 'cancelled' } : approval)
      : value.approvals,
  }
}

const fallbackApprovalValue = (value: Workflow, approvalId: string, decision: ApprovalDecision): Workflow => {
  const approvals = value.approvals.map(approval => approval.approval_id === approvalId
    ? { ...approval, status: decision === 'approve' ? 'approved' : 'rejected' }
    : approval)
  if (decision === 'reject') return { ...value, approvals }
  let advanced = false
  const steps = value.steps.map(step => {
    if (!advanced && step.status === 'waiting_approval') {
      advanced = true
      return { ...step, status: 'approved' }
    }
    return step
  })
  return {
    ...value,
    goal: { ...value.goal, status: 'queued' },
    workflow: { ...value.workflow, status: 'queued' },
    approvals,
    steps,
  }
}

const fallbackRetryValue = (value: Workflow, stepId: number): Workflow => ({
  ...value,
  business_outcome: null,
  goal: { ...value.goal, status: 'queued', business_outcome: null, error: undefined },
  workflow: { ...value.workflow, status: 'queued', business_outcome: null },
  steps: value.steps.map(step => step.id === stepId ? { ...step, status: 'queued', error: undefined } : step),
})

const actionFeedback = (action: WorkflowActionName) => ({
  start: '计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。',
  pause: '已请求暂停寻访，渠道会在当前查询单元结束后停止。',
  resume: '已请求继续寻访，工作流已进入执行队列。',
  cancel: '已确认停止本轮寻访。',
  archive: '工作流已归档。',
}[action])

export function useWorkflowWriteActions({
  sourceValue,
  reload,
  archived,
}: {
  sourceValue: Workflow
  reload: () => void | Promise<void>
  archived: () => void
}) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [feedback, setFeedback] = useState('')
  const [pendingAction, setPendingAction] = useState<ConfirmableWorkflowAction | null>(null)
  const [stored, setStored] = useState<StoredWorkflow | null>(null)
  const sourceRevision = workflowRevision(sourceValue)
  const value = stored?.sourceRevision === sourceRevision ? stored.value : sourceValue

  const storeWriteResult = (payload: unknown, fallback: Workflow) => {
    setStored({ sourceRevision, value: completeWorkflow(payload) || fallback })
  }

  const refreshAfterWrite = () => {
    void Promise.resolve().then(reload).catch(() => undefined)
  }

  const runAction = async (name: WorkflowActionName, payload: Record<string, unknown> = {}): Promise<boolean> => {
    if (busy) return false
    const exactPayload = name === 'start' && value.plan_ref
      ? { ...payload, expected_plan_version: value.plan_ref.version, expected_plan_hash: value.plan_ref.plan_hash }
      : payload
    setBusy(name)
    setError('')
    setFeedback('')
    try {
      const result = await api.workflowAction(value.workflow.workflow_id, name, exactPayload)
      if (name === 'archive') {
        archived()
      } else {
        storeWriteResult(result, fallbackActionValue(value, name))
        setFeedback(actionFeedback(name))
        refreshAfterWrite()
      }
      return true
    } catch (reason) {
      setError(humanizeActionError(reason, '工作流操作失败，请重试。'))
      return false
    } finally {
      setBusy('')
    }
  }

  // 确认链入口：pause/resume/cancel/archive 先弹确认卡（WorkflowActionConfirmDialog），
  // 用户在卡片上填原因并确认后才执行；确认时由 api.workflowAction 走
  // actions/preflight → write-confirmations/activate → 执行 的完整链路。
  const requestAction = (name: WorkflowActionName, payload: Record<string, unknown> = {}) => {
    if (busy) return
    if (CONFIRM_WORKFLOW_ACTIONS.has(name)) {
      setError('')
      setFeedback('')
      setPendingAction(name as ConfirmableWorkflowAction)
      return
    }
    void runAction(name, payload)
  }

  const cancelPendingAction = () => {
    if (!busy) setPendingAction(null)
  }

  // 确认卡提交：成功则关闭卡片；失败保留卡片并把错误展示在卡片内，可修正原因重试。
  const confirmPendingAction = async (note: string) => {
    const name = pendingAction
    if (!name || busy) return
    const succeeded = await runAction(name, note ? { note } : {})
    if (succeeded) setPendingAction(null)
  }

  const decide = async (approvalId: string, decision: ApprovalDecision) => {
    if (busy) return
    setBusy(approvalId)
    setError('')
    setFeedback('')
    try {
      const result = await api.approval(approvalId, decision)
      storeWriteResult(result, fallbackApprovalValue(value, approvalId, decision))
      setFeedback(decision === 'approve'
        ? '本次审批已批准，工作流已进入执行队列。'
        : '已选择不执行本次外部动作。')
      refreshAfterWrite()
    } catch (reason) {
      setError(humanizeActionError(reason, '审批失败，请重试。'))
    } finally {
      setBusy('')
    }
  }

  const retry = async (stepId: number) => {
    if (busy) return
    const key = `retry-${stepId}`
    setBusy(key)
    setError('')
    setFeedback('')
    try {
      const result = await api.retryStep(stepId)
      storeWriteResult(result, fallbackRetryValue(value, stepId))
      setFeedback('重试请求已提交，失败步骤已重新进入执行队列。')
      refreshAfterWrite()
    } catch (reason) {
      setError(humanizeActionError(reason, '重试失败，请稍后再试。'))
    } finally {
      setBusy('')
    }
  }

  return { value, busy, error, feedback, runAction, requestAction, pendingAction, cancelPendingAction, confirmPendingAction, decide, retry }
}
