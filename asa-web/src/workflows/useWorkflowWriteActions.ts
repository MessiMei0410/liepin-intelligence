import { useState } from 'react'
import { api, type Workflow } from '../api'
import { humanizeActionError } from '../shared/errors'

type WorkflowActionName = 'start' | 'pause' | 'resume' | 'cancel' | 'archive'
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
  const [stored, setStored] = useState<StoredWorkflow | null>(null)
  const sourceRevision = workflowRevision(sourceValue)
  const value = stored?.sourceRevision === sourceRevision ? stored.value : sourceValue

  const storeWriteResult = (payload: unknown, fallback: Workflow) => {
    setStored({ sourceRevision, value: completeWorkflow(payload) || fallback })
  }

  const refreshAfterWrite = () => {
    void Promise.resolve().then(reload).catch(() => undefined)
  }

  const runAction = async (name: WorkflowActionName, payload: Record<string, unknown> = {}) => {
    if (busy) return
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
    } catch (reason) {
      setError(humanizeActionError(reason, '工作流操作失败，请重试。'))
    } finally {
      setBusy('')
    }
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

  return { value, busy, error, feedback, runAction, decide, retry }
}
