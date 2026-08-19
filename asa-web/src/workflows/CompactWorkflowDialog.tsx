import { useState, type CSSProperties } from 'react'
import {
  Archive, Ban, Check, Circle, CircleCheck, Ellipsis, ExternalLink, FileText, GitBranch,
  ListChecks, LoaderCircle, Pause, Play, RotateCcw, ShieldCheck, TriangleAlert,
  UserRoundSearch, Workflow as WorkflowIcon, X,
} from 'lucide-react'
import type { Workflow } from '../api'
import { DialogPanel } from '../shared/Dialog'
import { nativeBridge } from '../shared/nativeBridge'
import type { DragResizeAnchor } from '../shared/dialogDragResize'
import { elapsed } from '../shared/format'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import { useWorkflowLiveSync } from './useWorkflowLiveSync'
import { useWorkflowWriteActions } from './useWorkflowWriteActions'
import { WorkflowActionConfirmDialog } from './WorkflowActionConfirmDialog'
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
  const [menu, setMenu] = useState<'view' | 'more' | ''>('')
  const {
    value: displayValue,
    busy,
    error,
    feedback: actionFeedback,
    runAction,
    requestAction,
    pendingAction,
    cancelPendingAction,
    confirmPendingAction,
    decide,
    retry,
  } = useWorkflowWriteActions({ sourceValue: value, reload, archived })
  const status = displayValue.workflow.status
  const live = activeWorkflowStatuses.has(status)
  const steps = displayValue.steps
  const pendingApprovals = displayValue.approvals.filter(item => item.status === 'pending')
  const failedSteps = steps.filter(step => ['failed', 'blocked'].includes(step.status))
  const completed = displayValue.progress?.completed ?? steps.filter(step => ['completed', 'skipped'].includes(step.status)).length
  const total = displayValue.progress?.total ?? steps.length
  const ratio = displayValue.progress?.ratio ?? completed / Math.max(1, total)
  const percent = Math.max(0, Math.min(100, Math.round(ratio * 100)))
  const businessOutcome = displayValue.business_outcome ?? displayValue.workflow.business_outcome ?? displayValue.goal.business_outcome
  const mapped = mapWorkflowStatus({ status, business_outcome: businessOutcome, steps })
  const activeStep = steps.find(step => ['running', 'queued', 'waiting_external', 'waiting_approval'].includes(step.status))
    || failedSteps[0]
    || steps.find(step => step.status === 'pending')
  const archiveAllowed = !activeWorkflowStatuses.has(status) && status !== 'paused' && !displayValue.workflow.archived_at

  const now = useWorkflowLiveSync(displayValue, reload)

  // 时长基准与 WorkflowPanel 对齐（2026-08-19 dogfood：审批等待中的工作流显示
  // 「已运行 389 小时」——那是从工作流首次启动起算的墙钟，含大量等待时间，误导）。
  // 待审批时以审批发起时间为基准显示「已等待」，只有真正在跑才显示「已运行」。
  const runningFor = status === 'waiting_approval' && pendingApprovals[0]?.created_at && now > 0
    ? `已等待 ${elapsed(pendingApprovals[0].created_at, undefined, now)}`
    : live && displayValue.workflow.started_at && now > 0
      ? `已运行 ${elapsed(displayValue.workflow.started_at, displayValue.workflow.finished_at, now)}`
      : live ? '运行中' : mapped.label

  // 弹出为独立窗口：macOS 宿主 openDetachedDialog 打开可自由拖出屏幕的原生窗口。
  const detachPanel = (anchor?: DragResizeAnchor): boolean => {
    if (nativeBridge('openDetachedDialog', { title: displayValue.goal.title, url: `/asa-app#workflow=${encodeURIComponent(displayValue.workflow.workflow_id)}&bare=1`, anchor })) {
      close()
      return true
    }
    return false
  }

  return <DialogPanel panelClassName="compact-workflow-dialog" ariaLabel={`工作流：${displayValue.goal.title}`} onEscape={close} onDetach={detachPanel} minWidth={320} minHeight={300}>
    <header className="compact-workflow-head" style={{ cursor: 'grab', userSelect: 'none', touchAction: 'none' }} title="按住拖动；拖出屏幕边缘可弹出为独立窗口">
      <span className="compact-workflow-icon"><WorkflowIcon /></span>
      <div>
        <h2>{displayValue.goal.title}</h2>
        <p>{mapped.label}{activeStep ? ` · ${activeStep.business_label}` : ''}</p>
      </div>
      <button className="icon-btn candidate-dialog-detach" onClick={() => void detachPanel()} title="弹出为独立窗口（可拖出屏幕）" aria-label="弹出为独立窗口"><ExternalLink /></button>
      <button className="icon-btn" onClick={close} title="关闭" aria-label="关闭"><X /></button>
    </header>

    <div className="compact-workflow-body">
      <ol className="compact-workflow-steps" aria-label="执行步骤">
        {steps.map(step => <li key={step.id} className={step.status} data-step-status={step.status}>
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
      {actionFeedback && <p className="compact-workflow-feedback" role="status"><CircleCheck />{actionFeedback}</p>}
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
            {live && <button role="menuitem" disabled={!!busy} onClick={() => { setMenu(''); requestAction('pause') }}><Pause />暂停寻访</button>}
            {status === 'paused' && <button role="menuitem" disabled={!!busy} onClick={() => { setMenu(''); requestAction('resume') }}><Play />继续寻访</button>}
            {!['cancelled', 'completed'].includes(status) && <button className="danger" role="menuitem" disabled={!!busy} onClick={() => { setMenu(''); requestAction('cancel') }}><Ban />立即停止寻访</button>}
            {archiveAllowed && <button role="menuitem" disabled={!!busy} onClick={() => { setMenu(''); requestAction('archive') }}><Archive />归档工作流</button>}
          </div>}
        </div>
      </div>
    </footer>
    {pendingAction && (
      <WorkflowActionConfirmDialog
        action={pendingAction}
        workflowTitle={displayValue.goal.title}
        busy={!!busy}
        error={error}
        onConfirm={note => void confirmPendingAction(note)}
        onCancel={cancelPendingAction}
      />
    )}
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
