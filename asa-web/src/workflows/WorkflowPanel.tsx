import { useEffect, useRef, useState } from 'react'
import { Activity, Archive, Ban, Check, ChevronLeft, CircleCheck, CircleDashed, Clock3, ListChecks, LoaderCircle, MessageSquareText, ShieldCheck, TriangleAlert, UserRoundSearch } from 'lucide-react'
import { api, Job, Workflow } from '../api'
import { summarySignature, workflowDetailSignature } from '../workflow/workflowSummary'
import { useWorkflowEventStream } from '../workflow/useWorkflowEventStream'
import { RevisePlanDialog } from '../components/RevisePlanDialog'
import { mapWorkflowStatus, workflowStatusLabel } from '../workflow/statusMapping'
import { date, elapsed } from '../shared/format'
import { recordValue } from '../shared/records'
import { humanizeActionError } from '../shared/errors'
import { SectionHead } from '../shared/primitives'
import { stepStatusLabel, activeWorkflowStatuses, stepTone, humanizeWorkflowError, humanizeWorkflowEvent, stepBusinessResult } from './utils'
import { openCopilotWindow } from '../copilot/bridge'
import { WorkflowTarget } from './WorkflowTarget'
import { WorkflowStrategy } from './WorkflowStrategy'
import { WorkflowCandidates } from './WorkflowCandidates'
import { WorkflowFunnel } from './WorkflowFunnel'
import { StrategyReview } from './StrategyReview'

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
  // S4-3c-4（N6）：strategy_v2.coverage_report（种子要素消费检查），旧策略/无原型为 null 不渲染
  const strategyCoverage=recordValue(recordValue(strategyStep?.output).strategy_v2).coverage_report
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
    <WorkflowStrategy strategy={strategy} channels={strategyChannels} gates={reviewGates} coverage={strategyCoverage} open={strategyOpen} toggle={()=>setStrategyOpen(value=>!value)}/>
    {sourcingStep&&<WorkflowFunnel workflowId={value.workflow.workflow_id} updatedAt={value.workflow.updated_at||''}/>}
    <StrategyReview workflowId={value.workflow.workflow_id} status={status} updatedAt={value.workflow.updated_at||''} openCandidate={openCandidate}/>
    <WorkflowCandidates ref={candidatesRef} workflowId={value.workflow.workflow_id} updatedAt={value.workflow.updated_at||''} sourcingStatus={sourcingStep?.status||'pending'} openCandidate={openCandidate}/>
    <div className="workflow-section-label"><ListChecks/><b>执行步骤</b><span>{completed}/{total} 已完成</span></div>
    <div className="step-list">{value.steps.map(s=>{const tone=stepTone(s.status);const detail=stepDetails[stepCacheKey(s)];const result=stepBusinessResult(detail||s);return <details className={`workflow-step ${tone}`} key={s.id} open={s.id===current?.id || s.status==='running'} onToggle={event=>{const isOpen=event.currentTarget.open;setExpandedSteps(prev=>{const next=new Set(prev);if(isOpen)next.add(s.id);else next.delete(s.id);return next});if(isOpen)void loadStep(s)}}><summary><span className={`step-no ${tone}`}><StepStatusIcon status={s.status} sequence={s.sequence}/></span><div><b>{s.business_label}</b><small>{s.reason}</small>{s.started_at&&<span className="step-time"><Clock3/>{s.status==='running'?`已执行 ${elapsed(s.started_at,s.finished_at,now)}`:`${date(s.started_at)}${s.finished_at?` · 用时 ${elapsed(s.started_at,s.finished_at,now)}`:''}`}</span>}</div><span className={`status-badge ${tone}`}>{s.risk_level} · {stepStatusLabel[s.status]||s.status}</span></summary><div className={`step-detail business-result ${result.error?'error':''}`}><b>{result.headline}</b>{result.facts.map((fact,index)=><span key={index}><Check/>{fact}</span>)}{!detail&&<span><LoaderCircle className="spin"/>完整执行详情加载中…</span>}{s.status==='failed'&&<button className="button" disabled={!!busy} onClick={()=>retry(s.id)}>{busy===`retry-${s.id}`?<LoaderCircle className="spin"/>:<Activity/>}重试此步骤</button>}</div></details>})}</div>
  </main><aside><SectionHead title="待审批" meta={`${pendingApprovals.length} 条`} />{pendingApprovals.length===0&&<div className="aside-empty"><ShieldCheck/><span>当前没有待审批动作</span></div>}{pendingApprovals.map(a=><div className={`approval ${a.status}`} key={a.approval_id}><div className="approval-title"><ShieldCheck/><div><b>{a.title}</b><span>{a.risk_level} · {a.preflight?.channel||'ASA'}</span></div></div>{a.preflight?.object_label&&<strong>{a.preflight.object_label}</strong>}{a.preflight?.before&&<p><span>批准前</span>{a.preflight.before}</p>}{a.preflight?.after&&<p><span>批准后</span>{a.preflight.after}</p>}<div className="approval-actions"><button disabled={!!busy} onClick={()=>decide(a.approval_id,'reject')}>拒绝</button><button className="approve" disabled={!!busy} onClick={()=>decide(a.approval_id,'approve')}>{busy===a.approval_id?<LoaderCircle className="spin"/>:<Check/>}批准寻访</button></div></div>)}
    <SectionHead title="执行动态" meta={`${events.length} 条`} />{events.length===0?<div className="aside-empty"><CircleDashed/><span>启动后将在这里显示过程</span></div>:<div className="workflow-events">{events.slice(0,12).map(e=><div key={e.id}><i className={stepTone(e.status)}/><span>{date(e.created_at)}</span><b>{humanizeWorkflowEvent(e)}</b></div>)}</div>}
    <SectionHead title="产物" meta={`${value.artifacts.length} 个`} />{value.artifacts.length===0&&<div className="aside-empty"><CircleDashed/><span>暂无执行产物</span></div>}{value.artifacts.map(a=><div className="aside-item" key={a.artifact_id}><b>{a.title}</b><small>{a.artifact_type} · {a.validation_status}</small></div>)}</aside></div></article>{reviseOpen&&<RevisePlanDialog workflowId={value.workflow.workflow_id} onCancel={()=>setReviseOpen(false)} onSubmit={revise}/>}</div>
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
