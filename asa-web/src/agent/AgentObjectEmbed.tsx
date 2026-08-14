import { useEffect, useState } from 'react'
import { Activity, Ban, BriefcaseBusiness, Building2, Check, ChevronDown, ChevronUp, CircleCheck, CircleDashed, ExternalLink, LoaderCircle, MapPin, Pause, Play, Target, UserRound, Workflow } from 'lucide-react'
import { api, CandidateDetail, JobDetail } from '../api'
import type { Workflow as WorkflowValue } from '../api'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import type { AgentReference } from './transport'
import { useDialogFocus } from '../shared/useDialogFocus'
import { dispatchCandidateUpdated } from '../shared/candidateEvents'

type CandidateAction = 'review' | 'advance' | 'contact' | 'recommend' | 'stop'
const actionLabels: Record<CandidateAction, string> = {
  review: '评分复核', advance: '复核通过', contact: '已联系', recommend: '已推荐', stop: '停止推进',
}
// 停止原因枚举与后端 asa_core/stop_reasons.py STOP_REASON_LABELS 逐字对齐（8 枚标准码）。
const stopReasons = [
  ['too_senior', '资历过高'], ['salary_mismatch', '薪资不符'], ['direction_mismatch', '方向不符'],
  ['experience_mismatch', '经验不符'], ['location_mismatch', '地点不符'], ['low_intent', '意向不足'],
  ['duplicate_candidate', '重复人选'], ['other', '其他'],
]

// 步骤级状态文案（工作流级状态一律走 statusMapping.ts 的 mapWorkflowStatus）。
const workflowStepStatus: Record<string, string> = {
  pending: '待执行', queued: '排队中', running: '执行中', waiting_approval: '待审批', waiting_external: '等渠道',
  completed: '已完成', skipped: '已跳过', failed: '失败', blocked: '阻塞', cancelled: '已取消',
}

const compactText = (value: unknown) => String(value || '').replace(/\s+/g, ' ').trim()
const businessText = (value: unknown) => {
  const text = compactText(value)
  return /[\u3400-\u9fff]/.test(text) ? text : ''
}
const recordValue = (value: unknown) => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const recordList = (value: unknown) => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>> : []
const textList = (value: unknown) => Array.isArray(value) ? value.map(item => compactText(item)).filter(Boolean) : []

function WorkflowPlanSummary({ progress, actionCard }: { progress?: Record<string, unknown> | null; actionCard?: Record<string, unknown> | null }) {
  const workflowId = compactText(progress?.workflow_id)
  const status = compactText(progress?.status || 'planned')
  const statusMapping = mapWorkflowStatus({ status, business_outcome: compactText(progress?.business_outcome) || null })
  const completed = Number(progress?.completed ?? 0)
  const total = Number(progress?.total ?? 0)
  const boundedTotal = Math.max(0, Number.isFinite(total) ? total : 0)
  const boundedCompleted = Math.min(boundedTotal || 0, Math.max(0, Number.isFinite(completed) ? completed : 0))
  const percent = boundedTotal ? Math.round((boundedCompleted / boundedTotal) * 100) : 0
  const approvals = recordList(progress?.pending_approvals).filter(item => compactText(item.status || 'pending') === 'pending')
  const evidence = recordList(actionCard?.evidence).filter(item => compactText(item.value)).slice(0, 4)
  const blocked = textList(actionCard?.blocked_reasons)
  const business = recordValue(actionCard?.business_summary)
  const task = compactText(business.task) || compactText(evidence.find(item => item.label === '理解目标')?.value) || compactText(progress?.label) || '准备执行'
  const current = compactText(business.current) || compactText(progress?.label) || compactText(evidence.find(item => item.label === '当前步骤')?.value) || '准备执行'
  if (!workflowId && !evidence.length && !boundedTotal) return null
  return <section className="agent-workflow-summary" aria-label="执行方案摘要">
    <div className="agent-workflow-summary-head"><b>本次要做什么</b><span>{statusMapping.label}</span></div>
    <p className="agent-workflow-task">{task}</p>
    {boundedTotal > 0 && <div className="agent-workflow-progress"><div className="agent-workflow-meter"><i style={{ width: `${percent}%` }}/></div><small>{boundedCompleted} / {boundedTotal} 步{approvals.length ? ` · ${approvals.length} 个审批待确认` : ''}</small></div>}
    <p className="agent-workflow-current"><b>{status === 'planned' ? '下一步' : '当前'}</b><span>{current}</span></p>
    {blocked.length > 0 && <p className="agent-workflow-risk">{blocked.join('；')}</p>}
  </section>
}

export function AgentObjectEmbed({ reference, workflowProgress, actionCard, onOpenFull }: {
  reference: AgentReference; workflowProgress?: Record<string, unknown> | null; actionCard?: Record<string, unknown> | null;
  onOpenFull: (reference: AgentReference) => void;
}) {
  const [expanded, setExpanded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [candidate, setCandidate] = useState<CandidateDetail>()
  const [job, setJob] = useState<JobDetail>()
  const [workflow, setWorkflow] = useState<WorkflowValue>()
  const [workflowSummary, setWorkflowSummary] = useState<Record<string, unknown> | null>(null)
  const [pending, setPending] = useState<{ action: CandidateAction; token: string; impact: string }>()
  const [workflowPending, setWorkflowPending] = useState<{ id: string; planRef: Record<string, unknown> }>()
  const actionDialogRef = useDialogFocus<HTMLElement>(Boolean(pending))
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('other')
  const [busy, setBusy] = useState('')
  const [actionFeedback, setActionFeedback] = useState('')
  const objectType = reference.type === 'job_candidate' ? 'candidate' : reference.type

  useEffect(() => {
    if (objectType !== 'workflow') return
    const workflowId = compactText(reference.id)
    if (!workflowId) return
    let active = true
    let timer: number | undefined
    const liveStatuses = new Set(['planned', 'queued', 'running', 'waiting_approval', 'waiting_external'])
    const loadSummary = async () => {
      let nextStatus = compactText(workflowProgress?.status || 'planned')
      try {
        const response = await fetch(`/api/v1/workflows/${encodeURIComponent(workflowId)}/summary`)
        if (!response.ok) return
        const payload = await response.json() as Record<string, unknown>
        if (!active) return
        const progress = recordValue(payload.progress)
        nextStatus = compactText(payload.status || nextStatus)
        if (compactText(payload.workflow_id) || compactText(payload.status) || Object.keys(progress).length) setWorkflowSummary(payload)
      } catch {
        // 旧 Core 或离线时继续使用消息里持久化的进度快照。
      } finally {
        if (active && liveStatuses.has(nextStatus)) timer = window.setTimeout(() => void loadSummary(), 3000)
      }
    }
    void loadSummary()
    return () => { active = false; if (timer) window.clearTimeout(timer) }
  }, [objectType, reference.id, workflowProgress?.status])

  const load = async () => {
    if (candidate || job || workflow) return
    const numericId = Number(reference.id)
    if (objectType !== 'workflow' && (String(reference.id).trim() === '' || !Number.isFinite(numericId))) {
      setError('对象 ID 无效，无法加载详情')
      return
    }
    setLoading(true); setError('')
    try {
      if (objectType === 'candidate') setCandidate((await api.candidate(numericId)).candidate)
      else if (objectType === 'job') setJob((await api.job(numericId)).job)
      else if (objectType === 'workflow') setWorkflow(await api.workflow(String(reference.id)))
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setLoading(false) }
  }
  const toggle = () => {
    const next = !expanded
    setExpanded(next)
    if (next) void load()
  }
  const preflight = async (action: CandidateAction) => {
    if (busy) return
    setBusy(action); setError(''); setActionFeedback('')
    try {
      const result = await api.preflight(Number(reference.id), action)
      setPending({ action, token: result.token, impact: result.impact })
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy('') }
  }
  const commit = async () => {
    if (!pending || busy) return
    const action = pending.action
    const candidateId = Number(reference.id)
    setBusy('commit'); setError(''); setActionFeedback('')
    try {
      const result = await api.commit(candidateId, action, pending.token, note.trim(), action === 'stop' ? reason : undefined)
      setPending(undefined); setNote('')
      setActionFeedback(result.already_applied || result.receipt?.idempotent_replay ? `${actionLabels[action]}此前已完成，已同步当前状态。` : `${actionLabels[action]}已完成。`)
      dispatchCandidateUpdated({ id: candidateId, stage: result.stage, isStopped: action === 'stop' })
      const refreshAfterWrite = async () => {
        const requests: Promise<unknown>[] = [
          api.candidate(candidateId).then(value => setCandidate(value.candidate)),
        ]
        if (candidate?.job_id) {
          requests.push(api.notifyFloatingCandidateUpdate(candidate.job_id, {
            job_candidate_id: candidateId,
            stage: result.stage,
            is_stopped: action === 'stop',
          }))
        }
        await Promise.allSettled(requests)
      }
      void refreshAfterWrite()
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value))
      try { setCandidate((await api.candidate(candidateId)).candidate) } catch { /* 保留原错误 */ }
    }
    finally { setBusy('') }
  }
  const decideApproval = async (approvalId: string, decision: 'approve' | 'reject') => {
    setBusy(approvalId); setError('')
    try {
      await api.approval(approvalId, decision)
      const workflowId = String(reference.id)
      const nextStatus = decision === 'approve' ? 'queued' : workflow?.workflow.status
      setWorkflow(current => current ? {
        ...current,
        workflow: { ...current.workflow, ...(nextStatus ? { status: nextStatus } : {}) },
        approvals: current.approvals.map(item => item.approval_id === approvalId
          ? { ...item, status: decision === 'approve' ? 'approved' : 'rejected' }
          : item),
      } : current)
      if (decision === 'approve') setWorkflowSummary(current => ({ ...recordValue(current), workflow_id: workflowId, status: 'queued' }))
      setActionFeedback(decision === 'approve' ? '本次审批已批准，工作流已进入执行队列。' : '已选择不执行本次外部动作。')
      void api.workflow(workflowId).then(setWorkflow).catch(() => undefined)
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy('') }
  }
  const updateWorkflowStatus = async (action: 'pause' | 'resume' | 'cancel') => {
    const workflowId = compactText(reference.id)
    if (!workflowId) return
    const status = action === 'pause' ? 'paused' : action === 'resume' ? 'waiting_external' : 'cancelled'
    const note = action === 'pause' ? '用户从 Agent 暂停寻访' : action === 'resume' ? '用户从 Agent 恢复寻访' : '用户从 Agent 立即停止寻访'
    setBusy(`workflow:${action}`); setError('')
    try {
      await api.workflowAction(workflowId, action, { note })
      if (workflow) setWorkflow({ ...workflow, workflow: { ...workflow.workflow, status } })
      setWorkflowSummary(current => current ? { ...current, status } : current)
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy('') }
  }
  const runWorkflowAction = async (action: Record<string, unknown>) => {
    const type = compactText(action.type)
    const workflowId = type === 'workflow_approval' ? compactText(reference.id) : compactText(action.id) || compactText(reference.id)
    if (!workflowId) return
    if (type === 'open_workflow' || type === 'workflow_approval') {
      onOpenFull({ type: 'workflow', id: workflowId, label: compactText(action.label) || reference.label })
      return
    }
    if (type !== 'start_workflow') return
    const planRef = action.plan_ref && typeof action.plan_ref === 'object' ? action.plan_ref as Record<string, unknown> : {}
    const version = Number(planRef.version)
    const planHash = compactText(planRef.plan_hash)
    const payload = Number.isFinite(version) && planHash ? { expected_plan_version: version, expected_plan_hash: planHash } : {}
    setWorkflowPending({ id: workflowId, planRef: payload })
  }
  const confirmWorkflowStart = async () => {
    if (!workflowPending || busy) return
    const { id: workflowId, planRef } = workflowPending
    setBusy('workflow:start'); setError('')
    try {
      await api.workflowAction(workflowId, 'start', planRef)
      setWorkflowPending(undefined)
      setWorkflow(current => current ? { ...current, workflow: { ...current.workflow, status: 'queued' } } : current)
      setWorkflowSummary(current => ({ ...recordValue(current), workflow_id: workflowId, status: 'queued' }))
      setActionFeedback('计划已确认并进入执行队列；外部寻访仍需 R3 单次审批。')
      void api.workflow(workflowId).then(setWorkflow).catch(() => undefined)
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy('') }
  }

  const Icon = objectType === 'candidate' ? UserRound : objectType === 'workflow' ? Workflow : BriefcaseBusiness
  const workflowStatus = workflow ? mapWorkflowStatus({
    status: workflow.workflow.status,
    business_outcome: workflow.workflow.business_outcome,
    steps: workflow.steps,
  }) : null
  const summaryProgress = workflowSummary ? recordValue(workflowSummary.progress) : {}
  const summaryNextStep = workflowSummary ? recordValue(workflowSummary.next_step) : {}
  const summaryStatus = compactText(workflowSummary?.status || workflowProgress?.status || 'planned')
  const displayedWorkflowProgress = workflowSummary ? {
    ...recordValue(workflowProgress),
    workflow_id: compactText(workflowSummary.workflow_id) || compactText(reference.id),
    status: summaryStatus,
    business_outcome: compactText(workflowSummary.business_outcome) || compactText(workflowProgress?.business_outcome) || null,
    completed: Number(summaryProgress.completed ?? workflowProgress?.completed ?? 0),
    total: Number(summaryProgress.total ?? workflowProgress?.total ?? 0),
    label: businessText(summaryNextStep.business_label) || businessText(workflowSummary.current_stage) || businessText(workflowProgress?.label) || (summaryStatus === 'completed' ? '任务已完成' : '准备执行'),
    pending_approvals: recordList(workflowSummary.pending_approvals),
  } : workflowProgress
  const displayedWorkflowStatus = compactText(displayedWorkflowProgress?.status || 'planned')
  const workflowInlineActions = objectType === 'workflow'
    ? recordList(actionCard?.next_actions).filter(action => {
      const type = compactText(action.type)
      if (!type) return false
      if (type === 'start_workflow') return displayedWorkflowStatus === 'planned'
      if (type === 'workflow_approval') return ['planned', 'waiting_approval'].includes(displayedWorkflowStatus)
      return true
    })
    : []
  const workflowIsActive = !!workflow && ['queued', 'running', 'waiting_approval', 'waiting_external'].includes(workflow.workflow.status)
  const workflowIsPaused = workflow?.workflow.status === 'paused'
  const rawWorkflowStage = compactText(workflow?.workflow.current_stage)
  const activeWorkflowStep = workflow?.steps.find(step => !['completed', 'skipped'].includes(step.status))
  const completedWorkflowStep = workflow ? [...workflow.steps].reverse().find(step => step.status === 'completed') : undefined
  const workflowStage = businessText(rawWorkflowStage)
    ? rawWorkflowStage
    : activeWorkflowStep?.business_label || completedWorkflowStep?.business_label || '准备执行'
  return <article className={`agent-object agent-object-${objectType} ${expanded ? 'expanded' : ''}`}>
    <button className="agent-object-toggle" onClick={toggle} aria-label={`${expanded ? '收起' : '展开'}${reference.label}`}>
      <Icon/><span><b>{reference.label}</b>{reference.subtitle && <small>{reference.subtitle}</small>}</span>{loading ? <LoaderCircle className="spin"/> : expanded ? <ChevronUp/> : <ChevronDown/>}
    </button>
    {objectType === 'workflow' && !expanded && <WorkflowPlanSummary progress={displayedWorkflowProgress} actionCard={actionCard}/>}
    {!expanded && workflowInlineActions.length > 0 && <div className="agent-object-actions agent-object-card-actions">{workflowInlineActions.map(action => {
      const type = compactText(action.type)
      const label = compactText(action.label) || (
        type === 'start_workflow'
          ? '开始执行本次任务'
          : type === 'workflow_approval'
            ? '查看审批'
            : '查看计划'
      )
      return <button key={`${type}:${compactText(action.id) || compactText(reference.id)}`} className={`button ${type === 'start_workflow' ? 'primary' : ''}`} disabled={!!busy} onClick={() => void runWorkflowAction(action)}>{busy === `workflow:${type}` ? <LoaderCircle className="spin"/> : type === 'start_workflow' ? <Activity/> : <ExternalLink/>}{label}</button>
    })}</div>}
    {actionFeedback && <p className="agent-inline-feedback" role="status">{actionFeedback}</p>}
    {expanded && <div className="agent-object-body">
      {error && <p className="agent-inline-error">{error}</p>}
      {candidate && <section className="agent-candidate-console" aria-label="候选人决策台">
        <header><span className="agent-console-icon"><UserRound/></span><div><small>当前经历</small><b>{candidate.current_company || '待补充'} · {candidate.current_title || '待补充'}</b></div><em className={candidate.is_stopped ? 'stopped' : ''}>{candidate.is_stopped ? '已停止' : candidate.clean_stage || '待确认'}</em></header>
        <div className="agent-candidate-facts"><span><MapPin/>{candidate.city || '地点待补充'}</span><span>{candidate.experience || '经验待补充'}</span><span>{candidate.education || '学历待补充'}</span></div>
        <div className="agent-candidate-target"><Target/><span><small>目标岗位</small><b>{candidate.client || '待确认客户'} · {candidate.job || '待确认岗位'}</b></span></div>
        {candidate.is_stopped && <p className="agent-console-notice danger">已确认停止，无需重复复核{candidate.stop_reason_label ? ` · ${candidate.stop_reason_label}` : ''}</p>}
        <div className="agent-object-actions agent-candidate-actions">
          {!candidate.is_stopped && (Object.keys(actionLabels) as CandidateAction[]).map(action => <button key={action} className={`button ${action === 'stop' ? 'danger' : ''}`} disabled={!!busy} onClick={() => void preflight(action)}>{busy === action ? <LoaderCircle className="spin"/> : action === 'stop' ? <Ban/> : <Check/>}{actionLabels[action]}</button>)}
          <button className="button" onClick={() => onOpenFull(reference)}><ExternalLink/>查看完整履历</button>
        </div>
      </section>}
      {job && <section className="agent-job-console" aria-label="岗位经营台">
        <header><span className="agent-console-icon"><Building2/></span><div><small>{job.client}</small><b>{job.title}</b></div><div className="agent-job-badges"><em>{job.priority || '常规'}</em><em>{job.status || '状态待确认'}</em></div></header>
        <div className="agent-job-funnel" aria-label="岗位人选漏斗"><div><b>{job.funnel.total}</b><span>全部</span></div><div><b>{job.funnel.active}</b><span>推进中</span></div><div><b>{job.funnel.contacted}</b><span>已触达</span></div><div><b>{job.funnel.recommended}</b><span>已推荐</span></div></div>
        <div className="agent-job-focus"><Target/><span><small>当前关注</small><b>{job.hard_requirements || job.latest_effective_strategy?.summary || job.summary || '岗位画像待完善'}</b></span></div>
        <div className="agent-object-actions"><button className="button primary" onClick={() => onOpenFull(reference)}><ExternalLink/>打开岗位工作台</button></div>
      </section>}
      {workflow && <section className="agent-workflow-console" aria-label="执行控制台">
        <header><div><small>当前状态</small><b>{workflowStatus?.label || '状态待同步'}</b></div><div><small>当前阶段</small><b>{workflowStage}</b></div></header>
        <ol aria-label="执行步骤">{workflow.steps.slice(0, 4).map(step => <li key={step.id} className={step.status}><span>{step.status === 'completed' ? <CircleCheck/> : <CircleDashed/>}</span><b>{step.business_label}</b><em>{workflowStepStatus[step.status] || '状态待同步'}</em></li>)}</ol>
        {workflow.steps.length > 4 && <small className="agent-workflow-more">另有 {workflow.steps.length - 4} 个步骤，请在完整详情中查看</small>}
        {workflow.approvals.filter(item => item.status === 'pending').map(item => <section className="agent-approval" key={item.approval_id}><b>{item.title}</b><span>{item.risk_level} · 单次授权</span><div><button className="button primary" disabled={!!busy} onClick={() => void decideApproval(item.approval_id, 'approve')}>{busy === item.approval_id && <LoaderCircle className="spin"/>}批准本次执行</button><button className="button" disabled={!!busy} onClick={() => void decideApproval(item.approval_id, 'reject')}>不执行</button></div></section>)}
        <div className="agent-object-actions">{workflowIsActive && <><button className="button" disabled={!!busy} onClick={() => void updateWorkflowStatus('pause')}>{busy === 'workflow:pause' ? <LoaderCircle className="spin"/> : <Pause/>}暂停</button><button className="button danger" disabled={!!busy} onClick={() => void updateWorkflowStatus('cancel')}>{busy === 'workflow:cancel' ? <LoaderCircle className="spin"/> : <Ban/>}结束本轮</button></>}{workflowIsPaused && <button className="button primary" disabled={!!busy} onClick={() => void updateWorkflowStatus('resume')}>{busy === 'workflow:resume' ? <LoaderCircle className="spin"/> : <Play/>}继续执行</button>}<button className="button" onClick={() => onOpenFull(reference)}><ExternalLink/>查看完整工作流</button></div>
      </section>}
    </div>}
    {pending && <div className="action-dialog-backdrop" role="presentation"><section ref={actionDialogRef} className="action-dialog agent-action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="agent-action-title">
      <header><span className={`action-dialog-icon ${pending.action === 'stop' ? 'danger' : ''}`}>{pending.action === 'stop' ? <Ban/> : <Check/>}</span><div><small>写入预检通过</small><h3 id="agent-action-title">{actionLabels[pending.action]}</h3></div><button className="icon-btn" aria-label="关闭" onClick={() => setPending(undefined)}>×</button></header>
      <div className="action-dialog-body"><p>{pending.impact}</p>{pending.action === 'stop' && <label><span>停止原因</span><select value={reason} onChange={event => setReason(event.target.value)}>{stopReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}<label><span>备注</span><textarea value={note} onChange={event => setNote(event.target.value)} /></label>{error && <div className="action-dialog-error">{error}</div>}</div>
      <footer><button className="button" onClick={() => setPending(undefined)}>取消</button><button className={`button ${pending.action === 'stop' ? 'danger-fill' : 'primary'}`} disabled={busy === 'commit'} onClick={() => void commit()}>{busy === 'commit' && <LoaderCircle className="spin"/>}确认执行</button></footer>
    </section></div>}
    {workflowPending && <div className="action-dialog-backdrop" role="presentation"><section className="action-dialog agent-action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="workflow-action-title">
      <header><span className="action-dialog-icon"><Activity/></span><div><small>执行预检</small><h3 id="workflow-action-title">确认开始执行</h3></div><button className="icon-btn" aria-label="关闭" onClick={() => setWorkflowPending(undefined)}>×</button></header>
      <div className="action-dialog-body"><p>将按当前计划启动工作流。外部寻访仍需单次审批，确认前不会开始执行。</p>{error && <div className="action-dialog-error">{error}</div>}</div>
      <footer><button className="button" onClick={() => setWorkflowPending(undefined)}>取消</button><button className="button primary" disabled={busy === 'workflow:start'} onClick={() => void confirmWorkflowStart()}>{busy === 'workflow:start' && <LoaderCircle className="spin"/>}确认开始</button></footer>
    </section></div>}
  </article>
}
