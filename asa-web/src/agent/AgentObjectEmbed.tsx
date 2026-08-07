import { useEffect, useState } from 'react'
import { Activity, Ban, BriefcaseBusiness, Check, ChevronDown, ChevronUp, ExternalLink, LoaderCircle, Pause, Play, UserRound, Workflow } from 'lucide-react'
import { api, CandidateDetail, JobDetail } from '../api'
import type { Workflow as WorkflowValue } from '../api'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import { WorkflowStrategy } from '../workflows/WorkflowStrategy'
import type { AgentReference } from './transport'
import { useDialogFocus } from '../shared/useDialogFocus'

type CandidateAction = 'review' | 'advance' | 'contact' | 'recommend' | 'stop'
const actionLabels: Record<CandidateAction, string> = {
  review: '评分复核', advance: '复核通过', contact: '已联系', recommend: '已推荐', stop: '停止推进',
}
const stopReasons = [
  ['too_senior', '太资深'], ['salary_too_high', '薪资太贵'], ['direction_mismatch', '方向不符'],
  ['experience_mismatch', '经验不符'], ['location_mismatch', '地点不符'], ['low_interest', '意愿低'],
  ['duplicate', '重复人选'], ['other', '其他'],
]

const workflowProgressStatus: Record<string, string> = {
  planned: '待确认计划', queued: '排队中', running: '执行中', waiting_approval: '待审批',
  waiting_external: '等待渠道回执', blocked: '已阻塞', failed: '技术失败',
  completed: '已完成', paused: '已暂停', cancelled: '已取消', superseded: '已被新修订替代',
}

const compactText = (value: unknown) => String(value || '').replace(/\s+/g, ' ').trim()
const recordValue = (value: unknown) => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const recordList = (value: unknown) => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>> : []
const textList = (value: unknown) => Array.isArray(value) ? value.map(item => compactText(item)).filter(Boolean) : []

function WorkflowPlanSummary({ progress, actionCard }: { progress?: Record<string, unknown> | null; actionCard?: Record<string, unknown> | null }) {
  const workflowId = compactText(progress?.workflow_id)
  const status = compactText(progress?.status || 'planned')
  const completed = Number(progress?.completed ?? 0)
  const total = Number(progress?.total ?? 0)
  const boundedTotal = Math.max(0, Number.isFinite(total) ? total : 0)
  const boundedCompleted = Math.min(boundedTotal || 0, Math.max(0, Number.isFinite(completed) ? completed : 0))
  const percent = boundedTotal ? Math.round((boundedCompleted / boundedTotal) * 100) : 0
  const approvals = recordList(progress?.pending_approvals).filter(item => compactText(item.status || 'pending') === 'pending')
  const evidence = recordList(actionCard?.evidence).filter(item => compactText(item.value)).slice(0, 4)
  const blocked = textList(actionCard?.blocked_reasons)
  if (!workflowId && !evidence.length && !boundedTotal) return null
  return <section className="agent-workflow-summary" aria-label="执行方案摘要">
    <div className="agent-workflow-summary-head"><b>推进方案</b><span>{workflowProgressStatus[status] || status || '状态待同步'}</span></div>
    <p>{compactText(progress?.label) || compactText(evidence.find(item => item.label === '当前步骤')?.value) || '准备执行'}</p>
    {boundedTotal > 0 && <><div className="agent-workflow-meter"><i style={{ width: `${percent}%` }}/></div><small>{boundedCompleted} / {boundedTotal} 步{approvals.length ? ` · ${approvals.length} 个审批待确认` : ''}</small></>}
    {evidence.length > 0 && <dl>{evidence.map(item => <div key={`${compactText(item.label)}:${compactText(item.value)}`}><dt>{compactText(item.label) || '依据'}</dt><dd>{compactText(item.value)}</dd></div>)}</dl>}
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
  const [strategyOpen, setStrategyOpen] = useState(false)
  const [pending, setPending] = useState<{ action: CandidateAction; token: string; impact: string }>()
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
    setBusy('commit'); setError(''); setActionFeedback('')
    try {
      const result = await api.commit(Number(reference.id), pending.action, pending.token, note.trim(), pending.action === 'stop' ? reason : undefined)
      setPending(undefined); setNote('')
      setCandidate((await api.candidate(Number(reference.id))).candidate)
      setActionFeedback(result.already_applied || result.receipt?.idempotent_replay ? `${actionLabels[pending.action]}此前已完成，已同步当前状态。` : `${actionLabels[pending.action]}已完成。`)
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value))
      try { setCandidate((await api.candidate(Number(reference.id))).candidate) } catch { /* 保留原错误 */ }
    }
    finally { setBusy('') }
  }
  const decideApproval = async (approvalId: string, decision: 'approve' | 'reject') => {
    setBusy(approvalId); setError('')
    try {
      await api.approval(approvalId, decision)
      setWorkflow(await api.workflow(String(reference.id)))
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
    setBusy(`workflow:${type}`); setError('')
    try {
      await api.workflowAction(workflowId, 'start', payload)
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy('') }
  }

  const Icon = objectType === 'candidate' ? UserRound : objectType === 'workflow' ? Workflow : BriefcaseBusiness
  const workflowStatus = workflow ? mapWorkflowStatus({
    status: workflow.workflow.status,
    business_outcome: workflow.workflow.business_outcome,
    steps: workflow.steps,
  }) : null
  const strategyStep = workflow?.steps.find(step => step.capability_id === 'search_strategy')
  const strategyOutput = recordValue(strategyStep?.output)
  const strategy = recordValue(strategyOutput.strategy)
  const strategyChannels = recordValue(strategy.channels)
  const reviewGates = recordValue(strategy.review_gates)
  const strategyV2 = recordValue(strategyOutput.strategy_v2)
  const workflowInlineActions = objectType === 'workflow'
    ? recordList(actionCard?.next_actions).filter(action => compactText(action.type))
    : []
  const summaryProgress = workflowSummary ? recordValue(workflowSummary.progress) : {}
  const summaryNextStep = workflowSummary ? recordValue(workflowSummary.next_step) : {}
  const displayedWorkflowProgress = workflowSummary ? {
    ...recordValue(workflowProgress),
    workflow_id: compactText(workflowSummary.workflow_id) || compactText(reference.id),
    status: compactText(workflowSummary.status) || compactText(workflowProgress?.status || 'planned'),
    completed: Number(summaryProgress.completed ?? workflowProgress?.completed ?? 0),
    total: Number(summaryProgress.total ?? workflowProgress?.total ?? 0),
    label: compactText(workflowSummary.current_stage) || compactText(summaryNextStep.business_label) || compactText(workflowProgress?.label) || '准备执行',
    pending_approvals: recordList(workflowSummary.pending_approvals),
  } : workflowProgress
  const workflowIsActive = !!workflow && ['queued', 'running', 'waiting_approval', 'waiting_external'].includes(workflow.workflow.status)
  const workflowIsPaused = workflow?.workflow.status === 'paused'
  return <article className={`agent-object ${expanded ? 'expanded' : ''}`}>
    <button className="agent-object-toggle" onClick={toggle} aria-label={`${expanded ? '收起' : '展开'}${reference.label}`}>
      <Icon/><span><b>{reference.label}</b>{reference.subtitle && <small>{reference.subtitle}</small>}</span>{loading ? <LoaderCircle className="spin"/> : expanded ? <ChevronUp/> : <ChevronDown/>}
    </button>
    {objectType === 'workflow' && <WorkflowPlanSummary progress={displayedWorkflowProgress} actionCard={actionCard}/>}
    {workflowInlineActions.length > 0 && <div className="agent-object-actions agent-object-card-actions">{workflowInlineActions.map(action => {
      const type = compactText(action.type)
      const label = compactText(action.label) || (
        type === 'start_workflow'
          ? '确认计划并准备'
          : type === 'workflow_approval'
            ? '查看审批'
            : '查看计划'
      )
      return <button key={`${type}:${compactText(action.id) || compactText(reference.id)}`} className={`button ${type === 'start_workflow' ? 'primary' : ''}`} disabled={!!busy} onClick={() => void runWorkflowAction(action)}>{busy === `workflow:${type}` ? <LoaderCircle className="spin"/> : type === 'start_workflow' ? <Activity/> : <ExternalLink/>}{label}</button>
    })}</div>}
    {expanded && <div className="agent-object-body">
      {error && <p className="agent-inline-error">{error}</p>}
      {actionFeedback && <p className="agent-inline-feedback" role="status">{actionFeedback}</p>}
      {candidate && <>
        <dl><div><dt>当前经历</dt><dd><span>{candidate.current_company || '待补充'}</span> · {candidate.current_title || '待补充'}</dd></div><div><dt>推进阶段</dt><dd>{candidate.clean_stage || '待确认'}</dd></div><div><dt>关联岗位</dt><dd>{candidate.client || '-'} / {candidate.job || '-'}</dd></div></dl>
        <div className="agent-object-actions">
          {!candidate.is_stopped && (Object.keys(actionLabels) as CandidateAction[]).map(action => <button key={action} className={`button ${action === 'stop' ? 'danger' : ''}`} disabled={!!busy} onClick={() => void preflight(action)}>{busy === action ? <LoaderCircle className="spin"/> : action === 'stop' ? <Ban/> : <Check/>}{actionLabels[action]}</button>)}
          <button className="button" onClick={() => onOpenFull(reference)}><ExternalLink/>完整详情</button>
        </div>
      </>}
      {job && <>
        <dl><div><dt>客户</dt><dd>{job.client}</dd></div><div><dt>优先级</dt><dd>{job.priority || '常规'}</dd></div><div><dt>人选漏斗</dt><dd>{job.funnel.active} 有效 / {job.funnel.total} 总计</dd></div><div><dt>当前策略</dt><dd>{job.latest_effective_strategy?.summary || job.summary || '待完善'}</dd></div></dl>
        <div className="agent-object-actions"><button className="button" onClick={() => onOpenFull(reference)}><ExternalLink/>完整详情</button></div>
      </>}
      {workflow && <>
        <dl><div><dt>状态</dt><dd>{workflowStatus?.label}</dd></div>{workflow.workflow.current_stage && <div><dt>当前阶段</dt><dd>{workflow.workflow.current_stage}</dd></div>}<div><dt>执行进度</dt><dd>{workflow.progress ? `${workflow.progress.completed} / ${workflow.progress.total}` : '准备中'}</dd></div></dl>
        {strategyStep && <WorkflowStrategy
          strategy={strategy}
          channels={strategyChannels}
          gates={reviewGates}
          coverage={strategyV2.coverage_report}
          open={strategyOpen}
          toggle={() => setStrategyOpen(value => !value)}
          strategyV2={strategyV2}
        />}
        {workflow.approvals.filter(item => item.status === 'pending').map(item => <section className="agent-approval" key={item.approval_id}><b>{item.title}</b><span>{item.risk_level} · 单次授权</span><div><button className="button primary" disabled={!!busy} onClick={() => void decideApproval(item.approval_id, 'approve')}>{busy === item.approval_id && <LoaderCircle className="spin"/>}批准本次执行</button><button className="button" disabled={!!busy} onClick={() => void decideApproval(item.approval_id, 'reject')}>不执行</button></div></section>)}
        <div className="agent-object-actions">{workflowIsActive && <><button className="button" disabled={!!busy} onClick={() => void updateWorkflowStatus('pause')}>{busy === 'workflow:pause' ? <LoaderCircle className="spin"/> : <Pause/>}暂停寻访</button><button className="button danger" disabled={!!busy} onClick={() => void updateWorkflowStatus('cancel')}>{busy === 'workflow:cancel' ? <LoaderCircle className="spin"/> : <Ban/>}立即停止寻访</button></>}{workflowIsPaused && <><button className="button primary" disabled={!!busy} onClick={() => void updateWorkflowStatus('resume')}>{busy === 'workflow:resume' ? <LoaderCircle className="spin"/> : <Play/>}继续寻访</button><button className="button danger" disabled={!!busy} onClick={() => void updateWorkflowStatus('cancel')}>{busy === 'workflow:cancel' ? <LoaderCircle className="spin"/> : <Ban/>}立即停止寻访</button></>}<button className="button" onClick={() => onOpenFull(reference)}><ExternalLink/>完整详情</button></div>
      </>}
    </div>}
    {pending && <div className="action-dialog-backdrop" role="presentation"><section ref={actionDialogRef} className="action-dialog agent-action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="agent-action-title">
      <header><span className={`action-dialog-icon ${pending.action === 'stop' ? 'danger' : ''}`}>{pending.action === 'stop' ? <Ban/> : <Check/>}</span><div><small>写入预检通过</small><h3 id="agent-action-title">{actionLabels[pending.action]}</h3></div><button className="icon-btn" aria-label="关闭" onClick={() => setPending(undefined)}>×</button></header>
      <div className="action-dialog-body"><p>{pending.impact}</p>{pending.action === 'stop' && <label><span>停止原因</span><select value={reason} onChange={event => setReason(event.target.value)}>{stopReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}<label><span>备注</span><textarea value={note} onChange={event => setNote(event.target.value)} /></label>{error && <div className="action-dialog-error">{error}</div>}</div>
      <footer><button className="button" onClick={() => setPending(undefined)}>取消</button><button className={`button ${pending.action === 'stop' ? 'danger-fill' : 'primary'}`} disabled={busy === 'commit'} onClick={() => void commit()}>{busy === 'commit' && <LoaderCircle className="spin"/>}确认执行</button></footer>
    </section></div>}
  </article>
}
