import { useEffect, useRef, useState } from 'react'
import { Activity, Archive, Ban, Check, ChevronLeft, CircleCheck, CircleDashed, Clock3, ExternalLink, FileText, ListChecks, LoaderCircle, MessageSquareText, Pause, Play, ShieldCheck, TriangleAlert, UserRoundSearch } from 'lucide-react'
import { api, Job, Workflow } from '../api'
import { summarySignature, workflowDetailSignature } from '../workflow/workflowSummary'
import { useWorkflowEventStream } from '../workflow/useWorkflowEventStream'
import { mapWorkflowStatus, workflowStatusLabel } from '../workflow/statusMapping'
import { date, elapsed } from '../shared/format'
import { recordValue } from '../shared/records'
import { useDraggableOverlay } from '../shared/useDraggableOverlay'
import { humanizeActionError } from '../shared/errors'
import { SectionHead } from '../shared/primitives'
import { stepStatusLabel, activeWorkflowStatuses, stepTone, humanizeWorkflowError, humanizeWorkflowEvent, stepBusinessResult } from './utils'
import { openAgentWorkspace } from '../agent/navigation'
import { WorkflowTarget } from './WorkflowTarget'
import { WorkflowStrategy } from './WorkflowStrategy'
import { WorkflowCandidates } from './WorkflowCandidates'
import { WorkflowFunnel } from './WorkflowFunnel'
import { StrategyReview } from './StrategyReview'
import { MappingTaskCard } from './MappingTaskCard'
import { findMappingArtifactId } from './mappingTask'
import { parseRevisionHighlights } from './revisionHighlight'
import { SourcingApprovalScope } from './SourcingApprovalScope'
import { WorkflowArtifactDialog } from './WorkflowArtifactDialog'
import { artifactAbsenceMessage, artifactStatusLabel, artifactTypeLabel } from './artifactPresentation'
import { BusinessDeliverySummary } from './BusinessDeliverySummary'
import { SourcingResultCard, type SourcingResultCardData } from './SourcingResultCard'

export function WorkflowPanel({value,jobs,close,reload,openCandidate,archived}:{value:Workflow,jobs:Job[],close:()=>void,reload:()=>void|Promise<void>,openCandidate:(id:number)=>void,archived:()=>void}) {
  const [busy,setBusy]=useState('')
  const [actionError,setActionError]=useState('')
  const [now,setNow]=useState(Date.now())
  const [strategyOpen,setStrategyOpen]=useState(false)
  const { overlayRef, panelRef, dragProps } = useDraggableOverlay()
  const [sourcingResultCard,setSourcingResultCard]=useState<SourcingResultCardData | null>(null)
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
  const archiveAllowed=!activeWorkflowStatuses.has(status)&&status!=='paused'&&!value.workflow.archived_at
  const events=value.events || []
  const strategyStep=value.steps.find(step=>step.capability_id==='search_strategy')
  const strategy=recordValue(recordValue(strategyStep?.output).strategy)
  const strategyChannels=recordValue(strategy.channels)
  const reviewGates=recordValue(strategy.review_gates)
  // S4-3c-4（N6）：strategy_v2.coverage_report（种子要素消费检查），旧策略/无原型为 null 不渲染
  const strategyV2=recordValue(recordValue(strategyStep?.output).strategy_v2)
  const strategyCoverage=strategyV2.coverage_report
  const sourcingStep=value.steps.find(step=>step.capability_id==='multi_channel_sourcing')
  // 按项编辑入口闸门与后端一致：寻访步骤离开 pending/waiting_approval/blocked/failed 即不可编辑
  const strategyEditable=['pending','waiting_approval','blocked','failed'].includes(sourcingStep?.status||'')
  const externalResult=recordValue(recordValue(sourcingStep?.output).external_result)
  const appliedResult=recordValue(recordValue(recordValue(externalResult.intake).applied))
  const assessmentStep=value.steps.find(step=>step.capability_id==='candidate_batch_assessment')
  const assessmentQueue=recordValue(recordValue(assessmentStep?.output).assessment_queue)
  const assessmentResultSummary=String(recordValue(assessmentStep?.output).summary||'').trim()
  const hasAssessmentResult=assessmentStep?.status==='completed'&&(Object.keys(assessmentQueue).length>0||!!assessmentResultSummary)
  const contextJobId=Number(value.goal.context?.id||strategy.job_id||0)
  // S5-2：Mapping 任务卡入口。已有任务（本工作流产物含 mapping_task）直接打开，不重复发起采集。
  const mappingArtifactId=findMappingArtifactId(value.artifacts)
  const [mappingCardId,setMappingCardId]=useState('')
  const [artifactCardId,setArtifactCardId]=useState('')
  const openMappingCard=(artifactId:string)=>setMappingCardId(artifactId)
  // 二期高亮：patch 修订落地后新增项闪 3 秒；按 workflow_id 只闪一次，防轮询重渲染反复闪
  const [flashedWorkflowId,setFlashedWorkflowId]=useState('')
  const revisionHighlights=parseRevisionHighlights(value.goal.context)
  const strategyHighlights=revisionHighlights.length&&flashedWorkflowId!==value.workflow.workflow_id?revisionHighlights:[]
  const jobEntity=jobs.find(job=>job.id===contextJobId)
  const target={client:String(strategy.client||appliedResult.client||jobEntity?.client||'客户待确认'),job:String(strategy.job||appliedResult.job||jobEntity?.title||'岗位待确认'),location:jobEntity?.location||'',status:jobEntity?.status||'',priority:jobEntity?.priority||'',id:contextJobId}

  useEffect(()=>{
    if(status!=='waiting_approval'||pendingApprovals.length===0)setActionError('')
  },[status,pendingApprovals.length])

  useEffect(()=>{
    if(revisionHighlights.length&&flashedWorkflowId!==value.workflow.workflow_id)setFlashedWorkflowId(value.workflow.workflow_id)
  },[value.workflow.workflow_id,revisionHighlights.length,flashedWorkflowId])

  // 寻访结果卡弹窗：工作流进入终局且未 dismissed 时自动展示。
  useEffect(()=>{
    const wfStatus=value.workflow.status
    let dismissed=''
    try { dismissed=localStorage.getItem('asa_dismissed_sourcing_result')||'' } catch { /* ignore */ }
    if(dismissed===value.workflow.workflow_id||sourcingResultCard||!['completed','blocked'].includes(wfStatus))return
    const sourcingArtifact=value.artifacts.find(a=>a.artifact_type==='sourcing_result')
    if(!sourcingArtifact)return
    const metadata=(sourcingArtifact as unknown as Record<string, unknown>).metadata
    const actionCard=metadata&&typeof metadata==='object'?(metadata as Record<string, unknown>).action_card:null
    if(actionCard&&typeof actionCard==='object'&&(actionCard as SourcingResultCardData).type==='sourcing_result'){
      setSourcingResultCard(actionCard as SourcingResultCardData)
    }
  },[value.workflow.status,value.workflow.workflow_id,value.artifacts,sourcingResultCard])

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
    const exactPayload=name==='start'&&value.plan_ref
      ? {...payload,expected_plan_version:value.plan_ref.version,expected_plan_hash:value.plan_ref.plan_hash}
      : payload
    setBusy(name);setActionError('')
    try{await api.workflowAction(value.workflow.workflow_id,name,exactPayload);if(name==='archive')archived();else await checkSummary()}
    catch(e){setActionError(humanizeActionError(e,'工作流操作失败，请重试。'))}
    finally{setBusy('')}
  }
  const discussStrategy=async()=>{
    setBusy('copilot')
    try{
      openAgentWorkspace({type:'workflow',id:value.workflow.workflow_id,mode:'strategy_revision',client:target.client,job:target.job})
    }finally{setBusy('')}
  }
  const openSourcingCandidatesNewTab=()=>{
    const url=`${location.origin}${location.pathname}#sourcing_candidates=${encodeURIComponent(value.workflow.workflow_id)}`
    window.open(url,'_blank','noopener,noreferrer')
  }
  const reviewCandidates=()=>{candidatesRef.current?.scrollIntoView?.({behavior:'smooth',block:'start'})}
  const decide=async(id:string,decision:string)=>{setBusy(id);setActionError('');try{await api.approval(id,decision);await checkSummary()}catch(e){setActionError(humanizeActionError(e,'审批失败，请重试。'))}finally{setBusy('')}}
  const retry=async(stepId:number)=>{setBusy(`retry-${stepId}`);setActionError('');try{await api.retryStep(stepId);await checkSummary()}catch(e){setActionError(humanizeActionError(e,'重试失败，请稍后再试。'))}finally{setBusy('')}}
  const headline=['target_met','needs_review','pool_insufficient'].includes(mapped.kind) ? mapped.label : status==='waiting_approval' ? `已完成 ${completed}/${total} 步，等待外部寻访授权` : status==='completed' ? `工作流已完成，共 ${total} 步` : status==='failed' ? humanizeWorkflowError(failedStep?.error||value.goal.error) : status==='blocked' ? '工作流需要处理后继续' : status==='paused' ? '已暂停，渠道会在当前查询单元结束后停止。' : status==='planned' ? '计划已就绪，等待确认' : current ? `正在处理：${current.business_label}` : workflowStatusLabel[status] || status
  return <div className="overlay" ref={overlayRef}><article ref={panelRef} className="workflow-panel"><header className="detail-head" style={{ cursor: 'grab', userSelect: 'none', touchAction: 'none' }} title="按住拖动" {...dragProps}><button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ChevronLeft/></button><div><h2>{value.goal.title}</h2><p>{value.workflow.workflow_id} · {mapped.label}</p></div><div className="detail-actions">{actionError&&<span className="tag warn">{actionError}</span>}<button className="button" disabled={!!busy} onClick={()=>void discussStrategy()}>{busy==='copilot'?<LoaderCircle className="spin"/>:<MessageSquareText/>}在 Agent 中讨论策略</button>{archiveAllowed&&<button className="button" disabled={!!busy} onClick={()=>action('archive')}>{busy==='archive'?<LoaderCircle className="spin"/>:<Archive/>}归档</button>}{live&&<button className="button" disabled={!!busy} onClick={()=>action('pause')}>{busy==='pause'?<LoaderCircle className="spin"/>:<Pause/>}暂停寻访</button>}{status==='paused'&&<button className="button primary" disabled={!!busy} onClick={()=>action('resume')}>{busy==='resume'?<LoaderCircle className="spin"/>:<Play/>}继续寻访</button>}{!['cancelled','completed'].includes(status)&&<button className="button danger" disabled={!!busy} onClick={()=>action('cancel')}>{busy==='cancel'?<LoaderCircle className="spin"/>:<Ban/>}立即停止寻访</button>}{status==='planned'&&<button className="button primary" disabled={!!busy} onClick={()=>action('start')}>{busy==='start'?<LoaderCircle className="spin"/>:<Activity/>}确认计划并准备</button>}</div></header><div className="workflow-body"><main>
    <section className={`workflow-progress ${progressTone}`} aria-live="polite">
      <div className="progress-status"><span className="progress-icon"><WorkflowStatusIcon status={status}/></span><div><span>{mapped.label}</span><b>{headline}</b><small>{status==='waiting_approval'&&pendingApprovals[0]?.created_at?`已等待 ${elapsed(pendingApprovals[0].created_at,undefined,now)}`:live&&value.workflow.started_at?`已运行 ${elapsed(value.workflow.started_at,value.workflow.finished_at,now)}`:`更新于 ${date(value.workflow.updated_at)}`}</small></div><strong>{percent}%</strong></div>
      <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><i style={{width:`${percent}%`}}/></div>
      <div className="progress-meta"><span>{completed} 步完成</span><span>{Math.max(0,total-completed)} 步待处理</span><span>{pendingApprovals.length?`${pendingApprovals.length} 项待审批`:'无需审批'}</span></div>
    </section>
    {mapped.showNextActions&&<div className="workflow-next-actions" role="group" aria-label="下一步操作"><button className="button" disabled={!!busy} onClick={reviewCandidates}><UserRoundSearch/>复核现有人选</button><button className="button" disabled={!!busy} onClick={openSourcingCandidatesNewTab}><ExternalLink/>新标签页打开寻访名单</button><button className="button" disabled={!!busy} onClick={()=>void discussStrategy()}>{busy==='copilot'&&<LoaderCircle className="spin"/>}在 Agent 中调整策略</button>{archiveAllowed&&<button className="button" disabled={!!busy} onClick={()=>action('archive')}>{busy==='archive'?<LoaderCircle className="spin"/>:<Archive/>}结束本轮</button>}</div>}
    <BusinessDeliverySummary workflow={value}/>
    <WorkflowTarget target={target} objective={value.goal.objective}/>
    <WorkflowStrategy strategy={strategy} channels={strategyChannels} gates={reviewGates} coverage={strategyCoverage} highlights={strategyHighlights} open={strategyOpen} toggle={()=>setStrategyOpen(value=>!value)} strategyV2={strategyV2} workflowId={value.workflow.workflow_id} editable={strategyEditable} onEdited={reload}/>
    {sourcingStep&&<WorkflowFunnel workflowId={value.workflow.workflow_id} updatedAt={value.workflow.updated_at||''}/>}
    <StrategyReview workflowId={value.workflow.workflow_id} status={status} updatedAt={value.workflow.updated_at||''} openCandidate={openCandidate} jobId={contextJobId||undefined} mappingArtifactId={mappingArtifactId||undefined} onOpenMapping={openMappingCard}/>
    {mappingCardId&&contextJobId>0&&<MappingTaskCard jobId={contextJobId} artifactId={mappingCardId} openCandidate={openCandidate} onClose={()=>setMappingCardId('')}/>}
    <WorkflowCandidates ref={candidatesRef} workflowId={value.workflow.workflow_id} updatedAt={value.workflow.updated_at||''} workflowStatus={status} sourcingStatus={sourcingStep?.status||'pending'} assessmentQueue={assessmentQueue} openCandidate={openCandidate}/>
    {sourcingResultCard&&<div className="sourcing-result-dialog-backdrop" role="dialog" aria-modal="true" aria-label={sourcingResultCard.title}>
      <div className="sourcing-result-dialog">
        <SourcingResultCard
          data={sourcingResultCard}
          onOpenCandidate={openCandidate}
          onOpenFullList={openSourcingCandidatesNewTab}
          onClose={()=>{setSourcingResultCard(null);try{localStorage.setItem('asa_dismissed_sourcing_result',value.workflow.workflow_id)}catch{/* ignore */}}}
          onAction={(actionType)=>{
            if(actionType==='review_candidates')reviewCandidates()
            else if(actionType==='discuss_strategy')discussStrategy()
            else if(actionType==='archive')void action('archive')
            else if(actionType==='continue_sourcing')discussStrategy()
            setSourcingResultCard(null)
          }}
        />
      </div>
    </div>}
    <div className="workflow-section-label"><ListChecks/><b>执行步骤</b><span>{completed}/{total} 已完成</span></div>
    <div className="step-list">{value.steps.map(s=>{const tone=stepTone(s.status);const detail=stepDetails[stepCacheKey(s)];const result=stepBusinessResult(detail||s);return <details className={`workflow-step ${tone}`} key={s.id} open={s.id===current?.id || s.status==='running'} onToggle={event=>{const isOpen=event.currentTarget.open;setExpandedSteps(prev=>{const next=new Set(prev);if(isOpen)next.add(s.id);else next.delete(s.id);return next});if(isOpen)void loadStep(s)}}><summary><span className={`step-no ${tone}`}><StepStatusIcon status={s.status} sequence={s.sequence}/></span><div><b>{s.business_label}</b><small>{s.reason}</small>{s.started_at&&<span className="step-time"><Clock3/>{s.status==='running'?`已执行 ${elapsed(s.started_at,s.finished_at,now)}`:`${date(s.started_at)}${s.finished_at?` · 用时 ${elapsed(s.started_at,s.finished_at,now)}`:''}`}</span>}</div><span className={`status-badge ${tone}`}>{s.risk_level} · {stepStatusLabel[s.status]||s.status}</span></summary><div className={`step-detail business-result ${result.error?'error':''}`}><b>{result.headline}</b>{result.facts.map((fact,index)=><span key={index}><Check/>{fact}</span>)}{!detail&&<span><LoaderCircle className="spin"/>完整执行详情加载中…</span>}{s.status==='failed'&&<button className="button" disabled={!!busy} onClick={()=>retry(s.id)}>{busy===`retry-${s.id}`?<LoaderCircle className="spin"/>:<Activity/>}重试此步骤</button>}</div></details>})}</div>
  </main><aside><SectionHead title="待审批" meta={`${pendingApprovals.length} 条`} />{pendingApprovals.length===0&&<div className="aside-empty"><ShieldCheck/><span>当前没有待审批动作</span></div>}{pendingApprovals.map(a=><div className={`approval ${a.status}`} key={a.approval_id}><div className="approval-title"><ShieldCheck/><div><b>{a.title}</b><span>{a.risk_level} · {a.preflight?.channel||'ASA'}</span></div></div>{a.preflight?.object_label&&<strong>{a.preflight.object_label}</strong>}{a.preflight?.before&&<p><span>批准前</span>{a.preflight.before}</p>}{a.preflight?.after&&<p><span>批准后</span>{a.preflight.after}</p>}<SourcingApprovalScope queryPlan={a.preflight?.query_plan_v1}/><div className="approval-actions"><button disabled={!!busy} onClick={()=>decide(a.approval_id,'reject')}>拒绝</button><button className="approve" disabled={!!busy} onClick={()=>decide(a.approval_id,'approve')}>{busy===a.approval_id?<LoaderCircle className="spin"/>:<Check/>}{a.risk_level==='R3'?'批准本次外部寻访':'批准执行'}</button></div></div>)}
    <SectionHead title="执行动态" meta={`${events.length} 条`} />{events.length===0?<div className="aside-empty"><CircleDashed/><span>启动后将在这里显示过程</span></div>:<div className="workflow-events">{events.slice(0,12).map(e=><div key={e.id}><i className={stepTone(e.status)}/><span>{date(e.created_at)}</span><b>{humanizeWorkflowEvent(e)}</b></div>)}</div>}
    <SectionHead title="结果与产物" meta={`${value.artifacts.length+(hasAssessmentResult?1:0)} 项`} />{hasAssessmentResult&&<div className="aside-item"><b>候选人核验结果</b><small>{assessmentResultSummary||`本轮评估 ${Number(assessmentQueue.started||0)} 位，岗位已评估 ${Number(assessmentQueue.completed||0)} 位`}</small></div>}{value.artifacts.length===0&&!hasAssessmentResult&&<div className="aside-empty artifact-empty"><CircleDashed/><span>{artifactAbsenceMessage(value)}</span></div>}{value.artifacts.map(a=><button type="button" className="aside-item artifact-item" key={a.artifact_id} onClick={()=>setArtifactCardId(a.artifact_id)} aria-label={`查看产物：${a.title}`}><FileText/><span><b>{a.title}</b><small>{artifactTypeLabel(a.artifact_type)} · {artifactStatusLabel(a.validation_status)}</small></span></button>)}</aside></div>{artifactCardId&&<WorkflowArtifactDialog key={artifactCardId} artifactId={artifactCardId} onClose={()=>setArtifactCardId('')}/>}</article></div>
}

function WorkflowStatusIcon({status}:{status:string}) {
  if(['queued','running','waiting_external'].includes(status))return <LoaderCircle className="spin"/>
  if(status==='waiting_approval')return <ShieldCheck className="pulse"/>
  if(status==='completed')return <CircleCheck/>
  if(['failed','blocked'].includes(status))return <TriangleAlert/>
  if(status==='cancelled')return <Ban/>
  if(status==='paused')return <Pause/>
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
