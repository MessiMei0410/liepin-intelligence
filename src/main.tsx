import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Activity, Archive, Ban, BriefcaseBusiness, Building2, Check, ChevronDown, ChevronLeft, ChevronRight, CircleCheck, CircleDashed, Clock3, ExternalLink, ListChecks, LoaderCircle, MapPin, MessageSquareText, Route, Search, Send, ShieldAlert, ShieldCheck, SquarePen, Target, TriangleAlert, UserRoundSearch, UsersRound, X } from 'lucide-react'
import { api, Bootstrap, BusinessFocus, Candidate, CandidateDetail, Dashboard, Job, JobDetail, Workflow } from './api'
import { summarySignature, workflowDetailSignature, WorkflowCandidateItem } from './workflow/workflowSummary'
import { useWorkflowEventStream } from './workflow/useWorkflowEventStream'
import { RevisePlanDialog } from './components/RevisePlanDialog'
import { mapWorkflowStatus, workflowStatusLabel } from './workflow/statusMapping'
import { Tab, tabs } from './shared/tabs'
import { date, elapsed, sourceLabel, sourceLinkLabel, eventStatusLabel } from './shared/format'
import { recordValue, arrayValue } from './shared/records'
import { copilotText } from './shared/text'
import { humanizeActionError } from './shared/errors'
import { SectionHead } from './shared/primitives'
import { stepStatusLabel, activeWorkflowStatuses, stepTone, humanizeWorkflowError, humanizeWorkflowEvent, stepBusinessResult } from './workflows/utils'
import { Overview } from './pages/Overview'
import { Jobs } from './pages/Jobs'
import { Candidates } from './pages/Candidates'
import { Progress } from './pages/Progress'
import { publishCopilotContext, openCopilotWindow } from './copilot/bridge'
import { JobPanel } from './panels/JobPanel'
import './styles.css'

type CopilotAction = { type: string; id?: string | number; label?: string }
type ChatMessage = { role: string; text: string; actions?: CopilotAction[] }
type CandidateAction = 'advance' | 'contact' | 'recommend' | 'stop'
type CandidateActionPreflight = { action: CandidateAction; token: string; impact: string; expires_at?: string }
const candidateActionLabels: Record<CandidateAction,string> = {
  advance:'复核通过', contact:'标记已联系', recommend:'标记已推荐', stop:'停止推进',
}

export function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [boot, setBoot] = useState<Bootstrap>()
  const [dashboard, setDashboard] = useState<Dashboard>()
  const [jobs, setJobs] = useState<Job[]>([])
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [query, setQuery] = useState('')
  const [job, setJob] = useState<JobDetail>()
  const [candidate, setCandidate] = useState<CandidateDetail>()
  const [workflow, setWorkflow] = useState<Workflow>()
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const candidateStateRef = useRef<CandidateDetail | undefined>(undefined)

  useEffect(() => {
    Promise.all([api.bootstrap(), api.dashboard(), api.jobs(), api.candidates()])
      .then(([b, d, j, c]) => { setBoot(b); setDashboard(d); setJobs(j.items); setCandidates(c.items); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [refreshKey])

  useEffect(() => {
    let active = true
    let refreshing = false
    const refreshDashboard = async () => {
      if (!active || refreshing || document.hidden) return
      refreshing = true
      try {
        const next = await api.dashboard()
        if (active) setDashboard(next)
      } catch {
        // The full bootstrap path surfaces persistent connection failures.
      } finally {
        refreshing = false
      }
    }
    const timer = window.setInterval(refreshDashboard, 2000)
    const onVisible = () => { if (!document.hidden) void refreshDashboard() }
    window.addEventListener('focus', refreshDashboard)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshDashboard)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  useEffect(() => { candidateStateRef.current = candidate }, [candidate])

  // R7：候选人变化只增量刷新候选人详情与列表，不再触发 bootstrap/jobs 全量重拉；
  // dashboard 计数由既有 2s 轮询自然收敛。
  const refreshCandidateList = async () => {
    try { setCandidates((await api.candidates()).items) } catch { /* 列表刷新失败时保留现状，下一轮再试。 */ }
  }

  useEffect(() => {
    const candidateId = candidate?.id
    if (!candidateId) return
    let active = true
    const refreshCandidate = async () => {
      try {
        const fresh = (await api.candidate(candidateId)).candidate
        if (!active || candidateStateRef.current?.id !== candidateId) return
        const current = candidateStateRef.current
        const changed = !current
          || current.updated_at !== fresh.updated_at
          || current.clean_stage !== fresh.clean_stage
          || current.raw_status !== fresh.raw_status
          || current.events.length !== fresh.events.length
        if (changed) {
          candidateStateRef.current = fresh
          setCandidate(fresh)
          void refreshCandidateList()
        }
      } catch {
        // Keep the current detail visible during a transient core restart.
      }
    }
    const timer = window.setInterval(refreshCandidate, 2000)
    window.addEventListener('focus', refreshCandidate)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshCandidate)
    }
  }, [candidate?.id])

  useEffect(() => {
    const openHash = () => {
      const hash = new URLSearchParams(location.hash.slice(1))
      const id = Number(hash.get('candidate'))
      const workflowId = hash.get('workflow')
      const jobId = Number(hash.get('job'))
      if (id) { openCandidate(id); return }
      if (workflowId) { openWorkflow(workflowId); return }
      if (jobId) { openJob(jobId); return }
      setJob(undefined); setCandidate(undefined); setWorkflow(undefined)
    }
    openHash(); addEventListener('hashchange', openHash); return () => removeEventListener('hashchange', openHash)
  }, [])

  const openCandidate = async (id: number) => {
    try { setJob(undefined); setWorkflow(undefined); setCandidate((await api.candidate(id)).candidate); location.hash = `candidate=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const refreshCandidateDetail = async (id: number) => {
    const fresh = (await api.candidate(id)).candidate
    candidateStateRef.current = fresh
    setCandidate(fresh)
    await refreshCandidateList()
  }
  const openJob = async (id: number) => {
    try { setCandidate(undefined); setWorkflow(undefined); setJob((await api.job(id)).job); setTab('jobs'); location.hash = `job=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const archiveWorkflow = async (id: string) => {
    try { await api.workflowAction(id, 'archive'); setRefreshKey(value => value + 1) }
    catch (e) { setError(humanizeActionError(e, '归档失败，请重试。')) }
  }
  const openWorkflow = async (id: string) => {
    try { setJob(undefined); setCandidate(undefined); setWorkflow(await api.workflow(id)); location.hash = `workflow=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const closeOverlay = () => { setJob(undefined); setCandidate(undefined); setWorkflow(undefined); history.replaceState(null, '', location.pathname) }
  const visibleJobs = jobs.filter(j => !query || `${j.id} ${j.client} ${j.title} ${j.status}`.toLowerCase().includes(query.toLowerCase()))
  const visibleCandidates = candidates.filter(c => !query || `${c.name} ${c.current_company} ${c.current_title} ${c.job} ${c.client}`.toLowerCase().includes(query.toLowerCase()))
  const copilotContext = candidate ? { type: 'candidate', id: candidate.id, candidate: candidate.name, client: candidate.client, job: candidate.job } : job ? { type: 'job', id: job.id, client: job.client, job: job.title } : workflow ? { type: 'workflow', id: workflow.workflow.workflow_id } : { type: 'page', page: tab }
  const copilotContextSignature = JSON.stringify(copilotContext)
  useEffect(() => {
    if (copilotContext.type === 'page') return
    void publishCopilotContext(copilotContext, 'selection', true)
  }, [copilotContextSignature])

  if (error && !boot) return <Diagnostics error={error} retry={() => setRefreshKey(x => x + 1)} />

  return <div className="shell">
    <aside className="nav">
      <div className="brand"><span>ASA</span><strong>Agent</strong><small>Recruiting OS</small></div>
      <nav>{tabs.map(([id, label, icon]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => { setTab(id); setQuery('') }}>{icon}<span>{label}</span></button>)}</nav>
      <a className="legacy" href="/admin/legacy" target="_blank">审计与旧版</a>
    </aside>
    <main className="main">
      <header className="topbar">
        <div><h1>{tabs.find(x => x[0] === tab)?.[1]}</h1><p>{boot?.core?.status === 'connected' ? 'ASA Agent 在线 · Core 已连接 · v3 实时数据' : 'ASA Agent 正在连接 Core'}</p></div>
        {(tab === 'jobs' || tab === 'candidates' || tab === 'progress') && <label className="search"><Search/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索姓名、公司或岗位" aria-label="搜索姓名、公司或岗位" /></label>}
        <button className="button copilot-launch" title="打开 ASA Copilot 浮窗" aria-label="打开 ASA Copilot 浮窗" onClick={()=>openCopilotWindow(copilotContext)}><MessageSquareText/><span>Copilot</span></button>
      </header>
      <div className="content">
        {tab === 'overview' && <Overview dashboard={dashboard} jobs={jobs} candidates={candidates} openWorkflow={openWorkflow} openCandidate={openCandidate} archiveWorkflow={archiveWorkflow} />}
        {tab === 'jobs' && <Jobs items={visibleJobs} onSelect={openJob} />}
        {tab === 'progress' && <Progress items={visibleCandidates} openCandidate={openCandidate} />}
        {tab === 'candidates' && <Candidates items={visibleCandidates} openCandidate={openCandidate} />}
      </div>
    </main>
    {job && <JobPanel value={job} close={closeOverlay} openCandidate={openCandidate} />}
    {candidate && <CandidatePanel value={candidate} close={closeOverlay} changed={() => refreshCandidateDetail(candidate.id)} />}
    {workflow && <WorkflowPanel value={workflow} jobs={jobs} close={closeOverlay} reload={() => openWorkflow(workflow.workflow.workflow_id)} openCandidate={openCandidate} archived={() => { closeOverlay(); setRefreshKey(value => value + 1) }} />}
    {error && <div className="toast"><ShieldAlert/> {error}<button onClick={() => setError('')}><X/></button></div>}
  </div>
}

export function CandidatePanel({value,close,changed}:{value:CandidateDetail,close:()=>void,changed:()=>void|Promise<void>}) {
  const [busy,setBusy]=useState('')
  const [feedback,setFeedback]=useState<{tone:'error'|'success';text:string}>()
  const [pendingAction,setPendingAction]=useState<CandidateActionPreflight>()
  const [actionNote,setActionNote]=useState('')
  const [view,setView]=useState<'overview'|'resume'|'activity'>('overview')
  const act=async(action:CandidateAction)=>{ setBusy(`preflight:${action}`); setFeedback(undefined); try { const pre=await api.preflight(value.id,action); setActionNote(''); setPendingAction({action,token:pre.token,impact:pre.impact,expires_at:pre.expires_at}) } catch(e) { setFeedback({tone:'error',text:copilotText(e)||'操作预检失败，请重试。'}) } finally { setBusy('') } }
  const commitAction=async()=>{ if(!pendingAction)return; const {action,token}=pendingAction; setBusy(`commit:${action}`); setFeedback(undefined); try { await api.commit(value.id,action,token,actionNote.trim()); setPendingAction(undefined); await changed(); setFeedback({tone:'success',text:`${candidateActionLabels[action]}已完成，候选人状态已更新。`}) } catch(e) { setFeedback({tone:'error',text:copilotText(e)||'操作提交失败，请重试。'}) } finally { setBusy('') } }
  const resume=value.resume
  const links=[...new Map(value.source_links.filter(x=>x.source_url).map(link=>[sourceLinkLabel(link.source_system),link])).values()]
  const stage=value.clean_stage||''
  const reviewPassed=['S2 ','S3 ','S7 ','S8 ','S9 ','S10 ','S11 ','S12 ','S13 '].some(prefix=>stage.startsWith(prefix))
  const contacted=['S3 ','S7 ','S8 ','S9 ','S10 ','S11 ','S12 ','S13 '].some(prefix=>stage.startsWith(prefix))
  const recommended=['S7 ','S8 ','S9 ','S10 ','S11 ','S12 ','S13 '].some(prefix=>stage.startsWith(prefix))
  return <div className="overlay"><article className="detail-panel candidate-panel"><header className="detail-head candidate-head"><div className="candidate-head-primary"><button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ChevronLeft/></button><div><h2>{value.name}</h2><p>{value.current_title || '职位待补充'} · {value.current_company || '公司待补充'} · {value.city || '城市待补充'}</p></div></div><div className="detail-actions"><button className="button" onClick={()=>openCopilotWindow({type:'candidate',id:value.id})}><MessageSquareText/>Copilot</button>{links.map(link=><a className="button" href={link.source_url} target="_blank" rel="noreferrer" title={`打开${sourceLinkLabel(link.source_system)}`} key={sourceLinkLabel(link.source_system)}>{sourceLinkLabel(link.source_system)}<ExternalLink/></a>)}{value.is_stopped?<span className="button danger"><Ban/>已停止推进</span>:<><button className="button" disabled={!!busy||reviewPassed} onClick={()=>act('advance')}>{busy==='preflight:advance'?<LoaderCircle className="spin"/>:reviewPassed?<Check/>:null}复核通过</button><button className="button" disabled={!!busy||contacted} onClick={()=>act('contact')}>{busy==='preflight:contact'?<LoaderCircle className="spin"/>:contacted?<Check/>:null}已联系</button><button className="button primary" disabled={!!busy||recommended} onClick={()=>act('recommend')}>{busy==='preflight:recommend'?<LoaderCircle className="spin"/>:recommended?<Check/>:null}已推荐</button><button className="button danger" disabled={!!busy} onClick={()=>act('stop')}>{busy==='preflight:stop'?<LoaderCircle className="spin"/>:<Ban/>}停止</button></>}</div></header>
      {feedback&&<div className={`candidate-action-feedback ${feedback.tone}`} role={feedback.tone==='error'?'alert':'status'}>{feedback.tone==='error'?<TriangleAlert/>:<CircleCheck/>}<span>{feedback.text}</span></div>}
      <div className="detail-body"><main><div className="candidate-main-content">
        <section className="resume-hero"><div><span>目标岗位</span><b>{value.client} · {value.job}</b></div><div><span>当前阶段</span><b>{value.clean_stage || value.flow_bucket || '待复核'}</b></div><div><span>经验 / 学历</span><b>{value.experience || '-'} · {value.education || '-'}</b></div></section>
        <nav className="candidate-tabs" aria-label="候选人详情"><button className={view==='overview'?'active':''} onClick={()=>setView('overview')}><UserRoundSearch/>概览</button><button className={view==='resume'?'active':''} onClick={()=>setView('resume')}><BriefcaseBusiness/>履历</button><button className={view==='activity'?'active':''} onClick={()=>setView('activity')}><Clock3/>记录</button></nav>
        {view==='overview'&&<>
          <ResumeOverview text={resume.summary || resume.full_text} company={value.current_company}/>
          {value.sourcing_attributions?.length>0&&<section className="sourcing-trace"><div className="sourcing-trace-head"><Search/><div><span>寻访来源</span><b>关键词及后续业务反馈</b></div></div>{value.sourcing_attributions.map(item=><div className="sourcing-trace-row" key={item.id}><div><span>{sourceLabel(item.channel)} · {item.source_round||'寻访查询'}</span><b>{item.source_query}</b><small>{item.source_purpose||'根据岗位策略生成'}{item.strategy_model?` · ${item.strategy_model}`:''}</small></div><div className={`learning-score ${Number(item.learning_score||0)<0?'negative':Number(item.learning_score||0)>0?'positive':''}`}><b>{Number(item.learning_score||0).toFixed(1)}</b><span>经验分</span></div><LearningSignals item={item}/></div>)}</section>}
        </>}
        {view==='resume'&&<div className="resume-workspace"><ResumeTimelineSection title="工作经历" text={resume.work_text} empty="尚未采集结构化工作经历，可通过来源链接核对原始简历。"/><ResumeTimelineSection title="项目经历" text={resume.project_text} empty="暂无结构化项目经历。"/><ResumeTimelineSection title="教育经历" text={resume.education_text} empty="暂无结构化教育经历。"/>{resume.full_text&&<details className="raw-resume"><summary>完整原始履历</summary><pre>{resume.full_text}</pre></details>}</div>}
        {view==='activity'&&<div className="candidate-records"><section className="resume-section"><h3>岗位关系</h3><div className="relation-list">{value.job_relations.map(r=><div key={r.id}><div><b>{r.job}</b><span>{r.client}</span></div><small>{r.clean_stage || r.flow_bucket}</small></div>)}</div></section><section className="resume-section"><h3>业务时间线</h3><div className="timeline timeline-main">{value.events.map(e=><div key={e.id}><i/><span>{date(e.event_time)}</span><b>{e.summary || e.event_type}</b><small>{eventStatusLabel(e.event_status)}</small></div>)}</div></section></div>}
      </div></main><aside><SectionHead title="岗位关系" meta={`${value.job_relations.length} 条`} />{value.job_relations.map(r=><div className="aside-item" key={r.id}><b>{r.job}</b><span>{r.client}</span><small>{r.clean_stage || r.flow_bucket}</small></div>)}<SectionHead title="最近动态" meta={`${value.events.length} 条`} /><div className="timeline">{value.events.slice(0,8).map(e=><div key={e.id}><i/><span>{date(e.event_time)}</span><b>{e.summary || e.event_type}</b><small>{eventStatusLabel(e.event_status)}</small></div>)}</div></aside></div>
    </article>{pendingAction&&<div className="action-dialog-backdrop" role="presentation"><section className="action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="candidate-action-title"><header><span className={`action-dialog-icon ${pendingAction.action==='stop'?'danger':''}`}>{pendingAction.action==='stop'?<Ban/>:<ShieldCheck/>}</span><div><small>操作确认</small><h3 id="candidate-action-title">{candidateActionLabels[pendingAction.action]}</h3></div><button className="icon-btn" disabled={busy.startsWith('commit:')} onClick={()=>{setPendingAction(undefined);setFeedback(undefined)}} title="取消" aria-label="取消"><X/></button></header><div className="action-dialog-body"><dl><div><dt>候选人</dt><dd>{value.name}</dd></div><div><dt>当前阶段</dt><dd>{value.clean_stage||value.flow_bucket||'待复核'}</dd></div></dl><p><ShieldCheck/>预检已通过：{pendingAction.impact}</p>{pendingAction.action==='stop'&&<label><span>停止备注（选填）</span><textarea value={actionNote} onChange={event=>setActionNote(event.target.value)} placeholder="例如：方向不符" rows={3}/></label>}{feedback?.tone==='error'&&<div className="action-dialog-error"><TriangleAlert/>{feedback.text}</div>}</div><footer><button className="button" disabled={busy.startsWith('commit:')} onClick={()=>{setPendingAction(undefined);setFeedback(undefined)}}>取消</button><button className={`button ${pendingAction.action==='stop'?'danger-fill':'primary'}`} disabled={busy.startsWith('commit:')} onClick={commitAction} autoFocus>{busy.startsWith('commit:')?<LoaderCircle className="spin"/>:pendingAction.action==='stop'?<Ban/>:<Check/>}确认{candidateActionLabels[pendingAction.action]}</button></footer></section></div>}</div>
}

function ResumeOverview({text,company}:{text?:string;company?:string}) {
  const normalized=String(text||'').replace(/\s+/g,' ').trim()
  if(!normalized)return <section className="resume-section"><h3>职业概览</h3><div className="empty">暂无职业概览。</div></section>
  const marker='求职期望：'
  const markerIndex=normalized.indexOf(marker)
  const companyIndex=company&&markerIndex>=0?normalized.indexOf(company,markerIndex+marker.length):-1
  const basic=markerIndex>=0?normalized.slice(0,markerIndex).trim():normalized
  const intent=markerIndex>=0?normalized.slice(markerIndex+marker.length,companyIndex>markerIndex?companyIndex:undefined).trim():''
  return <section className="resume-section resume-overview"><h3>职业概览</h3><dl><div><dt>基本信息</dt><dd>{basic.split(/\s+/).join(' · ')}</dd></div>{intent&&<div><dt>求职意向与关键词</dt><dd>{intent}</dd></div>}</dl></section>
}

function ResumeTimelineSection({title,text,empty}:{title:string;text?:string;empty:string}) {
  const items=String(text||'').split(/\n+/).map(item=>item.trim()).filter(Boolean)
  const groups=items.reduce<Array<{label:string;entries:string[]}>>((result,item)=>{const parts=item.split(/\s*·\s*/).map(part=>part.trim()).filter(Boolean);const label=parts[0]||item;const entry=parts.slice(1).join(' · ');const existing=result.find(group=>group.label===label);if(existing)existing.entries.push(entry);else result.push({label,entries:[entry]});return result},[])
  return <section className="resume-section"><h3>{title}<span>{items.length?`${groups.length} 组 · ${items.length} 段`:''}</span></h3>{groups.length?<div className="resume-timeline">{groups.map(group=><div key={group.label}><i/><div><b>{group.label}</b><div className="resume-timeline-entries">{group.entries.filter(Boolean).map((entry,index)=><span key={`${entry}-${index}`}>{entry}</span>)}</div></div></div>)}</div>:<div className="empty">{empty}</div>}</section>
}

function LearningSignals({item}:{item:CandidateDetail['sourcing_attributions'][number]}) {
  const signals=[['通过',item.review_pass_count],['联系',item.contacted_count],['推荐',item.recommended_count],['停止',item.stopped_count],['客户正向',item.client_positive_count],['客户否决',item.client_rejected_count]].filter(([,count])=>Number(count||0)>0)
  return <div className="learning-signals">{signals.length?signals.map(([label,count])=><span key={String(label)}>{label} {Number(count)}</span>):<span>暂无后续业务反馈</span>}</div>
}

export function WorkflowPanel({value,jobs,close,reload,openCandidate,archived}:{value:Workflow,jobs:Job[],close:()=>void,reload:()=>void|Promise<void>,openCandidate:(id:number)=>void,archived:()=>void}) {
  const [busy,setBusy]=useState('')
  const [actionError,setActionError]=useState('')
  const [now,setNow]=useState(Date.now())
  const [strategyOpen,setStrategyOpen]=useState(false)
  const [reviseOpen,setReviseOpen]=useState(false)
  const candidatesRef=useRef<HTMLElement>(null)
  const status=value.workflow.status
  const businessOutcome=value.business_outcome??value.workflow.business_outcome??value.goal.business_outcome
  const mapped=mapWorkflowStatus({status,business_outcome:businessOutcome,steps:value.steps})
  const progressTone=mapped.kind==='default'?stepTone(status):mapped.tone==='green'?'done':mapped.tone==='amber'?'needs-approval':mapped.tone==='red'?'error':''
  const live=activeWorkflowStatuses.has(status)
  const failedStep=value.steps.find(s=>s.status==='failed')
  const current=value.steps.find(s=>['running','waiting_approval','waiting_external','queued'].includes(s.status)) || failedStep || value.steps.find(s=>s.status==='pending')
  const completed=value.progress?.completed ?? value.steps.filter(s=>['completed','skipped'].includes(s.status)).length
  const total=value.progress?.total ?? value.steps.length
  const percent=Math.max(0,Math.min(100,Math.round((value.progress?.ratio ?? completed/Math.max(1,total))*100)))
  const pendingApprovals=value.approvals.filter(x=>x.status==='pending')
  const archiveAllowed=!activeWorkflowStatuses.has(status)&&!value.workflow.archived_at
  const events=value.events || []
  const strategyStep=value.steps.find(step=>step.capability_id==='search_strategy')
  const strategy=recordValue(recordValue(strategyStep?.output).strategy)
  const strategyChannels=recordValue(strategy.channels)
  const reviewGates=recordValue(strategy.review_gates)
  const sourcingStep=value.steps.find(step=>step.capability_id==='multi_channel_sourcing')
  const externalResult=recordValue(recordValue(sourcingStep?.output).external_result)
  const appliedResult=recordValue(recordValue(recordValue(externalResult.intake).applied))
  const contextJobId=Number(value.goal.context?.id||strategy.job_id||0)
  const jobEntity=jobs.find(job=>job.id===contextJobId)
  const target={client:String(strategy.client||appliedResult.client||jobEntity?.client||'客户待确认'),job:String(strategy.job||appliedResult.job||jobEntity?.title||'岗位待确认'),location:jobEntity?.location||'',status:jobEntity?.status||'',priority:jobEntity?.priority||'',id:contextJobId}

  useEffect(()=>{
    if(status!=='waiting_approval'||pendingApprovals.length===0)setActionError('')
  },[status,pendingApprovals.length])

  // R7 轮询减负：活跃工作流的轮询改打 summary 小路由（~2.5KB，原详情 ~195KB/次），
  // status/progress/business_outcome/pending_approvals/最近事件 任一变化才 reload 完整详情；
  // SSE 连接正常时轮询退化为 15s 兜底，事件到达即触发一次 summary 比对，断开自动回退 1.2s。
  const summarySigRef=useRef('')
  const summaryLockRef=useRef({running:false,pending:false})
  const checkSummaryRef=useRef<()=>Promise<void>>(async()=>undefined)
  const latestEventId=(value.events||[]).reduce((max,event)=>Math.max(max,event.id),0)
  const streamConnected=useWorkflowEventStream(live?value.workflow.workflow_id:undefined,latestEventId,()=>{void checkSummaryRef.current()})
  useEffect(()=>{summarySigRef.current=workflowDetailSignature(value)},[value])
  const checkSummary=async()=>{
    if(document.hidden)return
    const lock=summaryLockRef.current
    if(lock.running){lock.pending=true;return}
    lock.running=true
    try{
      do{
        lock.pending=false
        try{
          const next=await api.workflowSummary(value.workflow.workflow_id)
          const sig=summarySignature(next)
          if(sig!==summarySigRef.current){await reload();summarySigRef.current=sig}
        }catch{/* Core 短暂不可达：保留当前面板，下一轮再试。 */}
      }while(lock.pending)
    }finally{lock.running=false}
  }
  useEffect(()=>{checkSummaryRef.current=checkSummary})

  // R7 步骤详情按需：步骤区默认只渲染摘要级字段（标签/状态/时间/错误），展开某步时才调
  // /steps/{id} 拉完整 output（audit stdout、assessed_items），以 updated_at+status 为缓存键。
  const stepDetailsRef=useRef<Record<string,Workflow['steps'][number]>>({})
  const stepInflightRef=useRef(new Set<string>())
  const [stepDetails,setStepDetails]=useState<Record<string,Workflow['steps'][number]>>({})
  const [expandedSteps,setExpandedSteps]=useState<ReadonlySet<number>>(new Set())
  const stepCacheKey=(step:Workflow['steps'][number])=>`${step.id}:${step.updated_at||''}:${step.status}`
  const loadStep=async(step:Workflow['steps'][number])=>{
    const key=stepCacheKey(step)
    if(stepDetailsRef.current[key]||stepInflightRef.current.has(key))return
    stepInflightRef.current.add(key)
    try{
      const full=await api.workflowStep(value.workflow.workflow_id,step.id)
      stepDetailsRef.current={...stepDetailsRef.current,[key]:full}
    }catch{
      // 拉取失败时退回摘要级渲染，不阻断展开交互。
      stepDetailsRef.current={...stepDetailsRef.current,[key]:step}
    }finally{
      stepInflightRef.current.delete(key)
      setStepDetails(stepDetailsRef.current)
    }
  }
  useEffect(()=>{
    value.steps.filter(step=>step.id===current?.id||step.status==='running'||expandedSteps.has(step.id)).forEach(step=>{void loadStep(step)})
  },[value,expandedSteps])

  useEffect(()=>{
    if(!live)return
    const poll=window.setInterval(()=>void checkSummaryRef.current(),streamConnected?15000:1200)
    const clock=window.setInterval(()=>setNow(Date.now()),1000)
    const onVisible=()=>{if(!document.hidden)void checkSummaryRef.current()}
    document.addEventListener('visibilitychange',onVisible)
    return()=>{window.clearInterval(poll);window.clearInterval(clock);document.removeEventListener('visibilitychange',onVisible)}
  },[value.workflow.workflow_id,status,streamConnected])

  const action=async(name:string,payload:Record<string,unknown>={})=>{
    setBusy(name);setActionError('')
    try{await api.workflowAction(value.workflow.workflow_id,name,payload);if(name==='archive')archived();else await checkSummary()}
    catch(e){setActionError(humanizeActionError(e,'工作流操作失败，请重试。'))}
    finally{setBusy('')}
  }
  const revise=(instruction:string)=>{setReviseOpen(false);action('revise',{instruction})}
  const reviewCandidates=()=>{candidatesRef.current?.scrollIntoView?.({behavior:'smooth',block:'start'})}
  const decide=async(id:string,decision:string)=>{setBusy(id);setActionError('');try{await api.approval(id,decision);await checkSummary()}catch(e){setActionError(humanizeActionError(e,'审批失败，请重试。'))}finally{setBusy('')}}
  const retry=async(stepId:number)=>{setBusy(`retry-${stepId}`);setActionError('');try{await api.retryStep(stepId);await checkSummary()}catch(e){setActionError(humanizeActionError(e,'重试失败，请稍后再试。'))}finally{setBusy('')}}
  const headline=['target_met','needs_review','pool_insufficient'].includes(mapped.kind) ? mapped.label : status==='waiting_approval' ? `已完成 ${completed}/${total} 步，等待批准寻访` : status==='completed' ? `工作流已完成，共 ${total} 步` : status==='failed' ? humanizeWorkflowError(failedStep?.error||value.goal.error) : status==='blocked' ? '工作流需要处理后继续' : status==='planned' ? '计划已就绪，可以启动' : current ? `正在处理：${current.business_label}` : workflowStatusLabel[status] || status
  return <div className="overlay"><article className="workflow-panel"><header className="detail-head"><button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ChevronLeft/></button><div><h2>{value.goal.title}</h2><p>{value.workflow.workflow_id} · {mapped.label}</p></div><div className="detail-actions">{actionError&&<span className="tag warn">{actionError}</span>}<button className="button" onClick={()=>openCopilotWindow({type:'workflow',id:value.workflow.workflow_id})}><MessageSquareText/>Copilot</button>{archiveAllowed&&<button className="button" disabled={!!busy} onClick={()=>action('archive')}>{busy==='archive'?<LoaderCircle className="spin"/>:<Archive/>}归档</button>}{['planned','blocked','failed'].includes(status)&&<button className="button" disabled={!!busy} onClick={()=>setReviseOpen(true)}>{busy==='revise'&&<LoaderCircle className="spin"/>}修改计划</button>}{!['cancelled','completed'].includes(status)&&<button className="button" disabled={!!busy} onClick={()=>action('cancel')}>{busy==='cancel'&&<LoaderCircle className="spin"/>}取消</button>}{status==='planned'&&<button className="button primary" disabled={!!busy} onClick={()=>action('start')}>{busy==='start'?<LoaderCircle className="spin"/>:<Activity/>}启动</button>}</div></header><div className="workflow-body"><main>
    <section className={`workflow-progress ${progressTone}`} aria-live="polite">
      <div className="progress-status"><span className="progress-icon"><WorkflowStatusIcon status={status}/></span><div><span>{mapped.label}</span><b>{headline}</b><small>{status==='waiting_approval'&&pendingApprovals[0]?.created_at?`已等待 ${elapsed(pendingApprovals[0].created_at,undefined,now)}`:live&&value.workflow.started_at?`已运行 ${elapsed(value.workflow.started_at,value.workflow.finished_at,now)}`:`更新于 ${date(value.workflow.updated_at)}`}</small></div><strong>{percent}%</strong></div>
      <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><i style={{width:`${percent}%`}}/></div>
      <div className="progress-meta"><span>{completed} 步完成</span><span>{Math.max(0,total-completed)} 步待处理</span><span>{pendingApprovals.length?`${pendingApprovals.length} 项待审批`:'无需审批'}</span></div>
    </section>
    {mapped.showNextActions&&<div className="workflow-next-actions" role="group" aria-label="下一步操作"><button className="button" disabled={!!busy} onClick={reviewCandidates}><UserRoundSearch/>复核现有人选</button><button className="button" disabled={!!busy} onClick={()=>setReviseOpen(true)}>{busy==='revise'&&<LoaderCircle className="spin"/>}调整条件再搜</button>{archiveAllowed&&<button className="button" disabled={!!busy} onClick={()=>action('archive')}>{busy==='archive'?<LoaderCircle className="spin"/>:<Archive/>}结束本轮</button>}</div>}
    <WorkflowTarget target={target} objective={value.goal.objective}/>
    <WorkflowStrategy strategy={strategy} channels={strategyChannels} gates={reviewGates} open={strategyOpen} toggle={()=>setStrategyOpen(value=>!value)}/>
    <WorkflowCandidates ref={candidatesRef} workflowId={value.workflow.workflow_id} updatedAt={value.workflow.updated_at||''} sourcingStatus={sourcingStep?.status||'pending'} openCandidate={openCandidate}/>
    <div className="workflow-section-label"><ListChecks/><b>执行步骤</b><span>{completed}/{total} 已完成</span></div>
    <div className="step-list">{value.steps.map(s=>{const tone=stepTone(s.status);const detail=stepDetails[stepCacheKey(s)];const result=stepBusinessResult(detail||s);return <details className={`workflow-step ${tone}`} key={s.id} open={s.id===current?.id || s.status==='running'} onToggle={event=>{const isOpen=event.currentTarget.open;setExpandedSteps(prev=>{const next=new Set(prev);if(isOpen)next.add(s.id);else next.delete(s.id);return next});if(isOpen)void loadStep(s)}}><summary><span className={`step-no ${tone}`}><StepStatusIcon status={s.status} sequence={s.sequence}/></span><div><b>{s.business_label}</b><small>{s.reason}</small>{s.started_at&&<span className="step-time"><Clock3/>{s.status==='running'?`已执行 ${elapsed(s.started_at,s.finished_at,now)}`:`${date(s.started_at)}${s.finished_at?` · 用时 ${elapsed(s.started_at,s.finished_at,now)}`:''}`}</span>}</div><span className={`status-badge ${tone}`}>{s.risk_level} · {stepStatusLabel[s.status]||s.status}</span></summary><div className={`step-detail business-result ${result.error?'error':''}`}><b>{result.headline}</b>{result.facts.map((fact,index)=><span key={index}><Check/>{fact}</span>)}{!detail&&<span><LoaderCircle className="spin"/>完整执行详情加载中…</span>}{s.status==='failed'&&<button className="button" disabled={!!busy} onClick={()=>retry(s.id)}>{busy===`retry-${s.id}`?<LoaderCircle className="spin"/>:<Activity/>}重试此步骤</button>}</div></details>})}</div>
  </main><aside><SectionHead title="待审批" meta={`${pendingApprovals.length} 条`} />{pendingApprovals.length===0&&<div className="aside-empty"><ShieldCheck/><span>当前没有待审批动作</span></div>}{pendingApprovals.map(a=><div className={`approval ${a.status}`} key={a.approval_id}><div className="approval-title"><ShieldCheck/><div><b>{a.title}</b><span>{a.risk_level} · {a.preflight?.channel||'ASA'}</span></div></div>{a.preflight?.object_label&&<strong>{a.preflight.object_label}</strong>}{a.preflight?.before&&<p><span>批准前</span>{a.preflight.before}</p>}{a.preflight?.after&&<p><span>批准后</span>{a.preflight.after}</p>}<div className="approval-actions"><button disabled={!!busy} onClick={()=>decide(a.approval_id,'reject')}>拒绝</button><button className="approve" disabled={!!busy} onClick={()=>decide(a.approval_id,'approve')}>{busy===a.approval_id?<LoaderCircle className="spin"/>:<Check/>}批准寻访</button></div></div>)}
    <SectionHead title="执行动态" meta={`${events.length} 条`} />{events.length===0?<div className="aside-empty"><CircleDashed/><span>启动后将在这里显示过程</span></div>:<div className="workflow-events">{events.slice(0,12).map(e=><div key={e.id}><i className={stepTone(e.status)}/><span>{date(e.created_at)}</span><b>{humanizeWorkflowEvent(e)}</b></div>)}</div>}
    <SectionHead title="产物" meta={`${value.artifacts.length} 个`} />{value.artifacts.length===0&&<div className="aside-empty"><CircleDashed/><span>暂无执行产物</span></div>}{value.artifacts.map(a=><div className="aside-item" key={a.artifact_id}><b>{a.title}</b><small>{a.artifact_type} · {a.validation_status}</small></div>)}</aside></div></article>{reviseOpen&&<RevisePlanDialog onCancel={()=>setReviseOpen(false)} onSubmit={revise}/>}</div>
}

function WorkflowTarget({target,objective}:{target:{client:string;job:string;location:string;status:string;priority:string;id:number},objective:string}) {
  const openJob=()=>{if(target.id)location.hash=`job=${target.id}`}
  return <section className="workflow-insight workflow-target">
    <header><span className="insight-icon"><Target/></span><div><span>本次对应岗位</span><b>{target.client} / {target.job}</b></div>{target.id>0&&<button className="button" onClick={openJob}><BriefcaseBusiness/>打开岗位<ChevronRight/></button>}</header>
    <div className="target-facts"><div><Building2/><span>客户</span><b>{target.client}</b></div><div><BriefcaseBusiness/><span>岗位</span><b>{target.job}</b></div><div><MapPin/><span>状态</span><b>{target.location||'地点待确认'}{target.status?` · ${target.status}`:''}</b></div></div>
    <p>{objective}</p>{target.priority?.includes('P0')&&<span className="tag warn">P0 最急</span>}
  </section>
}

function WorkflowStrategy({strategy,channels,gates,open,toggle}:{strategy:Record<string,unknown>;channels:Record<string,unknown>;gates:Record<string,unknown>;open:boolean;toggle:()=>void}) {
  const liepin=arrayValue(channels.liepin)
  const xsaas=arrayValue(channels.xsaas)
  const hasStrategy=liepin.length+xsaas.length>0
  const generation=recordValue(strategy.generation)
  const renderQueries=(items:unknown[],label:string)=>{
    const visible=open?items:items.slice(0,3)
    return <div className="strategy-channel"><div className="strategy-channel-head"><b>{label}</b><span>{items.length} 组关键词</span></div><div className="strategy-queries">{visible.map((entry,index)=>{const item=recordValue(entry);return <div key={`${item.query}-${index}`}><b>{String(item.query||'关键词待补充')}</b><span>{String(item.purpose||'按岗位画像检索')}</span></div>})}</div>{!open&&items.length>3&&<small>另有 {items.length-3} 组关键词</small>}</div>
  }
  return <section className="workflow-insight workflow-strategy">
    <header><span className="insight-icon"><Route/></span><div><span>多渠道寻访策略</span><b>{hasStrategy?`猎聘 ${liepin.length} 组 · X-SaaS ${xsaas.length} 组`:'等待生成策略'}</b>{generation.mode&&<small>{generation.mode==='llm'?`大模型生成 · ${generation.model||'ASA Model'}`:'规则兜底'}{Number(generation.memory_hits||0)>0?` · 参考 ${generation.memory_hits} 条岗位记忆`:''}{Number(generation.experiment_count||0)>0?` · ${generation.experiment_count} 条历史实验`:''}</small>}</div>{hasStrategy&&<button className="button" onClick={toggle} aria-expanded={open}>{open?'收起策略':'查看完整策略'}<ChevronDown className={open?'rotate':''}/></button>}</header>
    {hasStrategy?<><div className="strategy-grid">{renderQueries(liepin,'猎聘')}{renderQueries(xsaas,'X-SaaS')}</div>{open&&<div className="strategy-rules"><StrategyRule title="硬性条件" values={arrayValue(gates.hard_requirements)} tone="required"/><StrategyRule title="排除规则" values={arrayValue(gates.negative_rules)} tone="excluded"/><StrategyRule title="风险提醒" values={arrayValue(gates.risk_points)} tone="risk"/></div>}</>:<div className="insight-empty">完成“生成多渠道寻访策略”后，这里会展示每个渠道的关键词与筛选规则。</div>}
  </section>
}

function StrategyRule({title,values,tone}:{title:string;values:unknown[];tone:string}) {
  if(!values.length)return null
  return <div className={`strategy-rule ${tone}`}><b>{title}</b><div>{values.map((value,index)=><span key={index}>{String(value)}</span>)}</div></div>
}

// R7：人选结果改走 /candidates 分页路由（摘要字段，total 在响应里），不再依赖详情大对象的 assessed_items。
// 首开/切换工作流拉第一页，"加载更多"增量翻页；父面板按需重载详情（updatedAt 变化）时静默刷新已加载窗口。
function WorkflowCandidates({workflowId,updatedAt,sourcingStatus,openCandidate,ref}:{workflowId:string;updatedAt:string;sourcingStatus:string;openCandidate:(id:number)=>void;ref?:React.Ref<HTMLElement>}) {
  const PAGE_SIZE=20
  const finished=['completed','failed','blocked'].includes(sourcingStatus)
  const itemsRef=useRef<WorkflowCandidateItem[]>([])
  const skipRefreshRef=useRef(true)
  const [items,setItems]=useState<WorkflowCandidateItem[]>([])
  const [total,setTotal]=useState(0)
  const [loading,setLoading]=useState(false)
  const fetchPage=async(offset:number,limit=PAGE_SIZE)=>{
    setLoading(true)
    try{
      const page=await api.workflowCandidates(workflowId,limit,offset)
      const next=offset?[...itemsRef.current,...page.items]:page.items
      itemsRef.current=next;setItems(next);setTotal(page.total)
    }catch{/* 接口暂不可用时保留已加载人选，下一次刷新再补。 */}
    finally{setLoading(false)}
  }
  useEffect(()=>{skipRefreshRef.current=true;itemsRef.current=[];setItems([]);setTotal(0);void fetchPage(0)},[workflowId])
  useEffect(()=>{
    if(skipRefreshRef.current){skipRefreshRef.current=false;return}
    void fetchPage(0,Math.min(200,Math.max(PAGE_SIZE,itemsRef.current.length)))
  },[updatedAt])
  const newCount=items.filter(item=>item.attribution?.from_workflow).length
  return <section ref={ref} className="workflow-insight workflow-candidates">
    <header><span className="insight-icon"><UsersRound/></span><div><span>人选结果</span><b>{finished?`本轮新增 ${newCount} 人 · 岗位已评估 ${total} 人`:'等待渠道与评估结果'}</b></div>{items.length>0&&<span className="tag warn">点击查看详情</span>}</header>
    {items.length?<div className="candidate-result-list">{items.map((candidate,index)=>{const score=Number(candidate.fit_score||0);const recommendation=candidate.recommendation==='not_recommended'?'不推荐':candidate.recommendation==='verify_first'?'待补证据':score>=75?'推荐':'待复核';return <button key={`${candidate.id}-${index}`} onClick={()=>openCandidate(candidate.id)}><span className="candidate-result-icon"><UserRoundSearch/></span><div><b>{candidate.name||'姓名待补充'}</b><span>{candidate.company||'公司待补充'} · {candidate.title||'职位待补充'}</span><small>{candidate.attribution?.from_workflow?'本轮新增':'历史入库'} · {sourceLabel(candidate.attribution?.channel||'')} · {candidate.fit_level||candidate.stage||'待评估'}</small></div><div className="candidate-score"><b>{score||'-'}</b><span>ASA 评估</span><small className={recommendation==='不推荐'?'bad':recommendation==='推荐'?'good':'warn'}>{recommendation}</small></div><ChevronRight/></button>})}</div>:<div className="insight-empty">{finished?'本轮没有新增人选，岗位也暂无评估结果。':'寻访执行并完成排重后，人选会显示在这里。'}</div>}
    {items.length<total&&<button className="button candidate-more" disabled={loading} onClick={()=>void fetchPage(items.length)}>{loading?<LoaderCircle className="spin"/>:<ChevronDown/>}加载更多（剩余 {total-items.length} 人）</button>}
  </section>
}

function WorkflowStatusIcon({status}:{status:string}) {
  if(['queued','running','waiting_external'].includes(status))return <LoaderCircle className="spin"/>
  if(status==='waiting_approval')return <ShieldCheck className="pulse"/>
  if(status==='completed')return <CircleCheck/>
  if(['failed','blocked'].includes(status))return <TriangleAlert/>
  if(status==='cancelled')return <Ban/>
  return <CircleDashed/>
}

function StepStatusIcon({status,sequence}:{status:string,sequence:number}) {
  if(['queued','running','waiting_external'].includes(status))return <LoaderCircle className="spin"/>
  if(status==='waiting_approval')return <ShieldCheck className="pulse"/>
  if(['completed','skipped'].includes(status))return <Check/>
  if(['failed','blocked'].includes(status))return <TriangleAlert/>
  if(status==='cancelled')return <Ban/>
  return <>{sequence}</>
}

function Copilot({context,openWorkflow,standalone=false}:{context:Record<string,unknown>,openWorkflow:(id:string)=>void|Promise<void>,standalone?:boolean}) {
  const [messages,setMessages]=useState<ChatMessage[]>([])
  const [text,setText]=useState('')
  const [busy,setBusy]=useState(false)
  const [actionBusy,setActionBusy]=useState('')
  const [sessionId,setSessionId]=useState(()=>{try{return localStorage.getItem('asa-copilot-session-id')||''}catch{return ''}})
  const [focus,setFocus]=useState<BusinessFocus>()
  const supportedAction=(action:CopilotAction)=>['start_workflow','open_workflow'].includes(action.type) && !!action.id
  const runAction=async(action:CopilotAction)=>{
    if(!supportedAction(action) || actionBusy)return
    const id=String(action.id)
    setActionBusy(`${action.type}:${id}`)
    try{
      if(action.type==='start_workflow'){
        await api.workflowAction(id,'start')
        setMessages(m=>[...m,{role:'asa',text:'已启动工作流，正在打开计划面板。'}])
      }
      await openWorkflow(id)
    }catch(e){
      setMessages(m=>[...m,{role:'asa',text:copilotText(e)||'工作流操作失败，请重试。'}])
    }finally{
      setActionBusy('')
    }
  }
  const actionsFrom=(value:unknown):CopilotAction[]=>Array.isArray(value)?value.filter((item):item is CopilotAction=>!!item&&typeof item==='object'&&supportedAction(item as CopilotAction)):[]
  useEffect(()=>{
    if(!sessionId)return
    let active=true
    api.copilotSession(sessionId).then(history=>{
      if(!active)return
      setMessages((history.messages||[]).map(item=>({role:item.role==='assistant'?'asa':'user',text:copilotText(item.content),actions:actionsFrom(item.suggested_actions)})))
      setFocus(history.business_focus)
    }).catch(()=>undefined)
    return()=>{active=false}
  },[sessionId])
  const newSession=()=>{
    setSessionId('');setMessages([]);setFocus(undefined);setText('')
    try{localStorage.removeItem('asa-copilot-session-id')}catch{/* Storage can be disabled. */}
  }
  const send=async()=>{
    if(!text.trim()||busy)return
    const q=text
    setText('')
    setMessages(m=>[...m,{role:'user',text:q}])
    setBusy(true)
    try{
      const r=await api.copilot(q,context,sessionId)
      const nextSession=String(r.session_id||sessionId)
      if(nextSession&&nextSession!==sessionId){setSessionId(nextSession);try{localStorage.setItem('asa-copilot-session-id',nextSession)}catch{/* Storage can be disabled. */}}
      setFocus(r.business_focus)
      const answer=copilotText(r.answer) || copilotText(r.message) || copilotText(r.response) || copilotText(r.summary)
      setMessages(m=>[...m,{role:'asa',text:answer||'已完成分析，请查看关联工作流与产物。',actions:actionsFrom(r.suggested_actions)}])
    }catch(e){
      setMessages(m=>[...m,{role:'asa',text:copilotText(e)||'Copilot 请求失败，请稍后重试。'}])
    }finally{
      setBusy(false)
    }
  }
  const contextLabel = context.type === 'candidate' ? `候选人 #${context.id}` : context.type === 'workflow' ? `工作流 ${context.id}` : `ASA Agent · ${tabs.find(item => item[0] === context.page)?.[1] || '总览'}`
  const focusLabel=focus?.candidate?.name||[focus?.client,focus?.job?.title].filter(Boolean).join(' / ')||focus?.client||''
  const actionLabel:Record<string,string>={job_archive:'归档岗位',job_split:'拆分岗位',job_publish:'发布岗位',candidate_sourcing:'寻访人选',candidate_outreach:'触达人选',candidate_review:'复核人选',recommendation:'客户推荐',salary:'谈薪处理'}
  return <aside className={`copilot ${standalone?'standalone':''}`}><header><MessageSquareText/><div><b>ASA Copilot</b><span>{contextLabel}</span></div><button className="icon-btn copilot-new" onClick={newSession} title="新建会话" aria-label="新建会话"><SquarePen/></button>{standalone&&<button className="icon-btn copilot-close" onClick={()=>window.close()} title="关闭浮窗" aria-label="关闭浮窗"><X/></button>}</header>{focusLabel&&<div className={`business-focus ${focus?.needs_clarification?'conflict':''}`}><span>{focus?.needs_clarification?'需要确认':'当前焦点'}</span><b>{focusLabel}</b>{focus?.action&&<small>{actionLabel[focus.action]||focus.action}{focus.directions?.length?` · ${focus.directions.join(' / ')}`:''}</small>}</div>}<div className="chat" aria-live="polite">{messages.length===0?<div className="chat-empty"><b>可以直接开始</b><span>询问当前岗位、人选或工作流。</span></div>:messages.map((m,i)=><div className={`message ${m.role}`} key={i}>{m.text}{!!m.actions?.length&&<div className="message-actions">{m.actions.map((action,index)=><button className={`button ${action.type==='start_workflow'?'primary':''}`} disabled={!!actionBusy} onClick={()=>runAction(action)} key={`${action.type}-${action.id}-${index}`}>{action.type==='start_workflow'?<Check/>:<ExternalLink/>}{action.label||'打开'}</button>)}</div>}</div>)}</div><div className="composer"><textarea value={text} onChange={e=>setText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}} placeholder="问 ASA…" aria-label="向 ASA 提问"/><button disabled={busy||!text.trim()} onClick={send} title="发送" aria-label="发送"><Send/></button></div></aside>
}

function CopilotSurface() {
  let context: Record<string, unknown> = { type: 'page', page: 'overview' }
  try {
    const value = JSON.parse(new URLSearchParams(location.search).get('context') || '{}')
    if (value && typeof value === 'object') context = value
  } catch { /* Keep the safe default context. */ }
  const openWorkflow = async (id: string) => {
    if (window.opener && !window.opener.closed) {
      window.opener.location.hash = `workflow=${id}`
      window.opener.focus()
      return
    }
    const agentUrl = new URL(location.href)
    agentUrl.search = ''
    agentUrl.hash = `workflow=${id}`
    window.open(agentUrl, 'asa-agent')
  }
  return <main className="copilot-surface"><Copilot context={context} openWorkflow={openWorkflow} standalone /></main>
}

function Diagnostics({error,retry}:{error:string,retry:()=>void}) { return <main className="diagnostics"><ShieldAlert/><h1>ASA Core 无法连接</h1><p>{error}</p><dl><dt>服务地址</dt><dd>http://127.0.0.1:8765/api/v1/health</dd><dt>数据策略</dt><dd>诊断期间不会显示演示数据或缓存人选。</dd></dl><button className="button primary" onClick={retry}>重新连接</button></main> }

const surface = new URLSearchParams(location.search).get('surface')
document.title = surface === 'copilot' ? 'ASA Copilot' : 'ASA Agent'
const rootElement = document.getElementById('root')
if (rootElement) createRoot(rootElement).render(<React.StrictMode>{surface === 'copilot' ? <CopilotSurface/> : <App />}</React.StrictMode>)
