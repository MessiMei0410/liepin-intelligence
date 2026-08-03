import { useState } from 'react'
import { Ban, BriefcaseBusiness, Check, ChevronDown, ChevronUp, ExternalLink, LoaderCircle, UserRound, Workflow } from 'lucide-react'
import { api, CandidateDetail, JobDetail } from '../api'
import type { Workflow as WorkflowValue } from '../api'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import type { AgentReference } from './transport'

type CandidateAction = 'review' | 'advance' | 'contact' | 'recommend' | 'stop'
const actionLabels: Record<CandidateAction, string> = {
  review: '评分复核', advance: '复核通过', contact: '已联系', recommend: '已推荐', stop: '停止推进',
}
const stopReasons = [
  ['too_senior', '太资深'], ['salary_too_high', '薪资太贵'], ['direction_mismatch', '方向不符'],
  ['experience_mismatch', '经验不符'], ['location_mismatch', '地点不符'], ['low_interest', '意愿低'],
  ['duplicate', '重复人选'], ['other', '其他'],
]

export function AgentObjectEmbed({ reference, onOpenFull }: { reference: AgentReference; onOpenFull: (reference: AgentReference) => void }) {
  const [expanded, setExpanded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [candidate, setCandidate] = useState<CandidateDetail>()
  const [job, setJob] = useState<JobDetail>()
  const [workflow, setWorkflow] = useState<WorkflowValue>()
  const [pending, setPending] = useState<{ action: CandidateAction; token: string; impact: string }>()
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('other')
  const [busy, setBusy] = useState('')
  const objectType = reference.type === 'job_candidate' ? 'candidate' : reference.type

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
    setBusy(action); setError('')
    try {
      const result = await api.preflight(Number(reference.id), action)
      setPending({ action, token: result.token, impact: result.impact })
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy('') }
  }
  const commit = async () => {
    if (!pending) return
    setBusy('commit'); setError('')
    try {
      await api.commit(Number(reference.id), pending.action, pending.token, note.trim(), pending.action === 'stop' ? reason : undefined)
      setPending(undefined); setNote('')
      setCandidate((await api.candidate(Number(reference.id))).candidate)
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
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

  const Icon = objectType === 'candidate' ? UserRound : objectType === 'workflow' ? Workflow : BriefcaseBusiness
  const workflowStatus = workflow ? mapWorkflowStatus({
    status: workflow.workflow.status,
    business_outcome: workflow.workflow.business_outcome,
    steps: workflow.steps,
  }) : null
  return <article className={`agent-object ${expanded ? 'expanded' : ''}`}>
    <button className="agent-object-toggle" onClick={toggle} aria-label={`${expanded ? '收起' : '展开'}${reference.label}`}>
      <Icon/><span><b>{reference.label}</b>{reference.subtitle && <small>{reference.subtitle}</small>}</span>{loading ? <LoaderCircle className="spin"/> : expanded ? <ChevronUp/> : <ChevronDown/>}
    </button>
    {expanded && <div className="agent-object-body">
      {error && <p className="agent-inline-error">{error}</p>}
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
        {workflow.approvals.filter(item => item.status === 'pending').map(item => <section className="agent-approval" key={item.approval_id}><b>{item.title}</b><span>{item.risk_level} · 单次授权</span><div><button className="button primary" disabled={!!busy} onClick={() => void decideApproval(item.approval_id, 'approve')}>{busy === item.approval_id && <LoaderCircle className="spin"/>}批准本次执行</button><button className="button" disabled={!!busy} onClick={() => void decideApproval(item.approval_id, 'reject')}>不执行</button></div></section>)}
        <div className="agent-object-actions"><button className="button" onClick={() => onOpenFull(reference)}><ExternalLink/>完整详情</button></div>
      </>}
    </div>}
    {pending && <div className="action-dialog-backdrop" role="presentation"><section className="action-dialog agent-action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="agent-action-title">
      <header><span className={`action-dialog-icon ${pending.action === 'stop' ? 'danger' : ''}`}>{pending.action === 'stop' ? <Ban/> : <Check/>}</span><div><small>写入预检通过</small><h3 id="agent-action-title">{actionLabels[pending.action]}</h3></div><button className="icon-btn" aria-label="关闭" onClick={() => setPending(undefined)}>×</button></header>
      <div className="action-dialog-body"><p>{pending.impact}</p>{pending.action === 'stop' && <label><span>停止原因</span><select value={reason} onChange={event => setReason(event.target.value)}>{stopReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}<label><span>备注</span><textarea value={note} onChange={event => setNote(event.target.value)} /></label>{error && <div className="action-dialog-error">{error}</div>}</div>
      <footer><button className="button" onClick={() => setPending(undefined)}>取消</button><button className={`button ${pending.action === 'stop' ? 'danger-fill' : 'primary'}`} disabled={busy === 'commit'} onClick={() => void commit()}>{busy === 'commit' && <LoaderCircle className="spin"/>}确认执行</button></footer>
    </section></div>}
  </article>
}
