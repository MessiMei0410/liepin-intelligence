import { useState, type CSSProperties } from 'react'
import {
  Archive, Ban, Check, Circle, CircleCheck, Ellipsis, FileText, GitBranch,
  ListChecks, LoaderCircle, Pause, Play, RotateCcw, ShieldCheck, TriangleAlert,
  UserRoundSearch, Workflow as WorkflowIcon, X,
} from 'lucide-react'
import { api, type Workflow } from '../api'
import { DialogPanel } from '../shared/Dialog'
import { humanizeActionError } from '../shared/errors'
import { elapsed } from '../shared/format'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import { useWorkflowLiveSync } from './useWorkflowLiveSync'
import { activeWorkflowStatuses, humanizeWorkflowError, stepStatusLabel } from './utils'

export type WorkflowDetailSection = 'strategy' | 'candidates' | 'funnel' | 'events' | 'artifacts' | 'full'

export const detailItems: Array<{ id: WorkflowDetailSection; label: string; icon: typeof GitBranch }> = [
  { id: 'strategy', label: '寻访策略', icon: GitBranch },
  { id: 'candidates', label: '人选名单', icon: UserRoundSearch },
  { id: 'funnel', label: '渠道漏斗', icon: ListChecks },
  { id: 'events', label: '执行动态', icon: RotateCcw },
  { id: 'artifacts', label: '结果与产物', icon: FileText },
  { id: 'full', label: '完整详情', icon: WorkflowIcon },
]

export function CompactWorkflowDialog({
  value, close, reload, archived, openDetail,
}: {
  value: Workflow
  close: () => void
  reload: () => void | Promise<void>
  archived: () => void
  openDetail: (section: WorkflowDetailSection) => void
}) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [menu, setMenu] = useState<'view' | 'more' | ''>('')
  const status = value.workflow.status
  const live = activeWorkflowStatuses.has(status)
  const pendingApprovals = value.approvals.filter(item => item.status === 'pending')
  const failedSteps = value.steps.filter(step => ['failed', 'blocked'].includes(step.status))
  const completed = value.progress?.completed ?? value.steps.filter(step => ['completed', 'skipped'].includes(step.status)).length
  const total = value.progress?.total ?? value.steps.length
  const ratio = value.progress?.ratio ?? completed / Math.max(1, total)
  const percent = Math.max(0, Math.min(100, Math.round(ratio * 100)))
  const businessOutcome = value.business_outcome ?? value.workflow.business_outcome ?? value.goal.business_outcome
  const mapped = mapWorkflowStatus({ status, business_outcome: businessOutcome, steps: value.steps })
  const activeStep = value.steps.find(step => ['running', 'queued', 'waiting_external', 'waiting_approval'].includes(step.status))
    || failedSteps[0]
    || value.steps.find(step => step.status === 'pending')
  const archiveAllowed = !activeWorkflowStatuses.has(status) && status !== 'paused' && !value.workflow.archived_at

  const now = useWorkflowLiveSync(value, reload)

  const runAction = async (name: string, payload: Record<string, unknown> = {}) => {
    const exactPayload = name === 'start' && value.plan_ref
      ? { ...payload, expected_plan_version: value.plan_ref.version, expected_plan_hash: value.plan_ref.plan_hash }
      : payload
    setBusy(name)
    setError('')
    setMenu('')
    try {
      await api.workflowAction(value.workflow.workflow_id, name, exactPayload)
      if (name === 'archive') archived()
      else await reload()
    } catch (reason) {
      setError(humanizeActionError(reason, '工作流操作失败，请重试。'))
    } finally {
      setBusy('')
    }
  }

  const decide = async (approvalId: string, decision: 'approve' | 'reject') => {
    setBusy(approvalId)
    setError('')
    try {
      await api.approval(approvalId, decision)
      await reload()
    } catch (reason) {
      setError(humanizeActionError(reason, '审批失败，请重试。'))
    } finally {
      setBusy('')
    }
  }

  const retry = async (stepId: number) => {
    const key = `retry-${stepId}`
    setBusy(key)
    setError('')
    try {
      await api.retryStep(stepId)
      await reload()
    } catch (reason) {
      setError(humanizeActionError(reason, '重试失败，请稍后再试。'))
    } finally {
      setBusy('')
    }
  }

  const runningFor = live && value.workflow.started_at && now > 0
    ? `已运行 ${elapsed(value.workflow.started_at, value.workflow.finished_at, now)}`
    : live ? '运行中' : mapped.label

  return <DialogPanel panelClassName="compact-workflow-dialog" ariaLabel={`工作流：${value.goal.title}`} onEscape={close} minWidth={320} minHeight={300}>
    <header className="compact-workflow-head">
      <span className="compact-workflow-icon"><WorkflowIcon /></span>
      <div>
        <h2>{value.goal.title}</h2>
        <p>{mapped.label}{activeStep ? ` · ${activeStep.business_label}` : ''}</p>
      </div>
      <button className="icon-btn" onClick={close} title="关闭" aria-label="关闭"><X /></button>
    </header>

    <div className="compact-workflow-body">
      <ol className="compact-workflow-steps" aria-label="执行步骤">
        {value.steps.map(step => <li key={step.id} className={step.status} data-step-status={step.status}>
          <span className="compact-step-icon"><CompactStepIcon status={step.status} /></span>
          <div><b>{step.business_label}</b>{step.reason && <small>{step.reason}</small>}</div>
          <em>{stepStatusLabel[step.status] || '状态待同步'}</em>
        </li>)}
      </ol>

      {pendingApprovals.map(approval => <section className="compact-workflow-approval" key={approval.approval_id} aria-label="待审批操作">
        <ShieldCheck />
        <div><b>{approval.title}</b><span>{approval.risk_level} · {approval.preflight?.channel || 'ASA'} · 单次授权</span></div>
        <div className="compact-approval-actions">
          <button className="button" disabled={!!busy} onClick={() => void decide(approval.approval_id, 'reject')}>不执行</button>
          <button className="button primary" disabled={!!busy} onClick={() => void decide(approval.approval_id, 'approve')}>
            {busy === approval.approval_id ? <LoaderCircle className="spin" /> : <Check />}
            {approval.risk_level === 'R3' ? '批准本次寻访' : '批准执行'}
          </button>
        </div>
      </section>)}

      {failedSteps.map(step => <section className="compact-workflow-failure" key={step.id}>
        <TriangleAlert />
        <div><b>{step.business_label}</b><span>{humanizeWorkflowError(step.error)}</span></div>
        <button className="button" disabled={!!busy} onClick={() => void retry(step.id)}>
          {busy === `retry-${step.id}` ? <LoaderCircle className="spin" /> : <RotateCcw />}重试
        </button>
      </section>)}

      {error && <p className="compact-workflow-error" role="alert">{error}</p>}
    </div>

    <footer className="compact-workflow-foot">
      <div className="compact-progress" aria-label={`工作流进度：${completed}/${total} 步`}>
        <span className="compact-progress-ring"><i style={{ '--workflow-progress': `${percent * 3.6}deg` } as CSSProperties} /></span>
        <b>第 {Math.min(total, completed + (completed < total ? 1 : 0))} / {total} 步</b>
        <span>{runningFor}</span>
        <strong>{percent}%</strong>
      </div>
      <div className="compact-workflow-controls">
        {status === 'planned' && <button className="button primary" disabled={!!busy} onClick={() => void runAction('start')}>
          {busy === 'start' ? <LoaderCircle className="spin" /> : <Play />}确认计划并准备
        </button>}
        <div className="compact-menu-wrap">
          <button className="button" aria-haspopup="menu" aria-expanded={menu === 'view'} onClick={() => setMenu(current => current === 'view' ? '' : 'view')}>查看</button>
          {menu === 'view' && <div className="compact-workflow-menu" role="menu">
            {detailItems.map(item => <button key={item.id} role="menuitem" onClick={() => openDetail(item.id)}><item.icon />{item.label}</button>)}
          </div>}
        </div>
        <div className="compact-menu-wrap">
          <button className="icon-btn" aria-label="更多工作流操作" aria-haspopup="menu" aria-expanded={menu === 'more'} onClick={() => setMenu(current => current === 'more' ? '' : 'more')}><Ellipsis /></button>
          {menu === 'more' && <div className="compact-workflow-menu align-right" role="menu">
            {live && <button role="menuitem" disabled={!!busy} onClick={() => void runAction('pause')}><Pause />暂停寻访</button>}
            {status === 'paused' && <button role="menuitem" disabled={!!busy} onClick={() => void runAction('resume')}><Play />继续寻访</button>}
            {!['cancelled', 'completed'].includes(status) && <button className="danger" role="menuitem" disabled={!!busy} onClick={() => void runAction('cancel')}><Ban />立即停止寻访</button>}
            {archiveAllowed && <button role="menuitem" disabled={!!busy} onClick={() => void runAction('archive')}><Archive />归档工作流</button>}
          </div>}
        </div>
      </div>
    </footer>
  </DialogPanel>
}

function CompactStepIcon({ status }: { status: string }) {
  if (['completed', 'skipped'].includes(status)) return <CircleCheck />
  if (['running', 'queued', 'waiting_external'].includes(status)) return <LoaderCircle className="spin" />
  if (status === 'waiting_approval') return <ShieldCheck className="pulse" />
  if (['failed', 'blocked'].includes(status)) return <TriangleAlert />
  if (status === 'cancelled') return <Ban />
  if (status === 'paused') return <Pause />
  return <Circle />
}
