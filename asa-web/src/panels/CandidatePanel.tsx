import { useState } from 'react'
import { Ban, BriefcaseBusiness, CalendarPlus, Check, ChevronLeft, CircleCheck, ClipboardCheck, Clock3, ExternalLink, FileText, LoaderCircle, MessageSquareText, Search, ShieldCheck, TriangleAlert, UserRoundSearch, X } from 'lucide-react'
import { api, CandidateDetail } from '../api'
import { CandidateAssessment } from './CandidateAssessment'
import { date, sourceLabel, sourceLinkLabel, eventStatusLabel, lifecycleEventLabel, lifecycleEventTone } from '../shared/format'
import { copilotText } from '../shared/text'
import { SectionHead } from '../shared/primitives'
import { openAgentWorkspace } from '../agent/navigation'
import { DialogPanel } from '../shared/Dialog'
import type { DragResizeAnchor } from '../shared/dialogDragResize'
import { nativeBridge } from '../shared/nativeBridge'
import { buildResumeOverview, formatFeedbackScore } from './overviewFormat'
import { parseEducationDetails, parseProjectDetails, parseWorkDetails } from './resumeDetail'
import { WorkflowArtifactDialog } from '../workflows/WorkflowArtifactDialog'
import { artifactStatusLabel, artifactTypeLabel } from '../workflows/artifactPresentation'
import { RecommendationConfirmCard, RecommendationDecisionFields, recordRecommendationDecision, recommendationPackageNote } from './RecommendationDecision'
import type { RecommendationDecisionRecord } from './RecommendationDecision'
import { RecommendationPackagesSection } from './RecommendationPackages'
import { LifecycleEventForm } from './LifecycleEventForm'
import { useDialogFocus } from '../shared/useDialogFocus'
import { dispatchCandidateUpdated } from '../shared/candidateEvents'

export type CandidateAction = 'advance' | 'review' | 'contact' | 'recommend' | 'stop'
export type CandidateActionPreflight = { action: CandidateAction; token: string; consultant_token?: string; impact: string; expires_at?: string }
const candidateActionLabels: Record<CandidateAction,string> = {
  advance:'复核通过', review:'评分复核', contact:'标记已联系', recommend:'标记已推荐', stop:'停止推进',
}
// S4-5（N5）评分复核快捷记录：复盘"评估尺度复核"的顾问结论（尺太严/人不行）作为 note 经既有
// preflight→commit 链路写入候选人事件（action=review，后端既有通道 candidate_review_requested），
// 不新开写路径；结论前缀【评分复核】便于时间线检索与后续校准回看。
// R10 停止原因枚举：与 Core 契约一致（POST /api/v1/candidate-actions/commit 的 reason 字段，8 枚举；
// 未知值后端降级 other 并保留原文；GET /api/v1/candidates/stop-reasons/summary 返回同套枚举+中文标签）。
// 硬编码而非运行时拉取：枚举是契约常量，省去额外请求与加载态，labels 即后端中文文案。
const stopReasonOptions: Array<{code:string;label:string}> = [
  {code:'too_senior',label:'资历过高'},
  {code:'salary_mismatch',label:'薪资不符'},
  {code:'direction_mismatch',label:'方向不符'},
  {code:'experience_mismatch',label:'经验不符'},
  {code:'location_mismatch',label:'地点不符'},
  {code:'low_intent',label:'意向不足'},
  {code:'duplicate_candidate',label:'重复人选'},
  {code:'other',label:'其他'},
]

export function CandidatePanel({value,close,changed}:{value:CandidateDetail,close:()=>void,changed:()=>void|Promise<void>}) {
  const [busy,setBusy]=useState('')
  const [feedback,setFeedback]=useState<{tone:'error'|'success';text:string}>()
  const [pendingAction,setPendingAction]=useState<CandidateActionPreflight>()
  const [actionNote,setActionNote]=useState('')
  const [actionReason,setActionReason]=useState('other')
  const [view,setView]=useState<'overview'|'resume'|'activity'|'assessment'>('overview')
  const [artifactCardId,setArtifactCardId]=useState('')
  const [recommendDecision,setRecommendDecision]=useState<RecommendationDecisionRecord>()
  const [lifecycleFormOpen,setLifecycleFormOpen]=useState(false)
  useDialogFocus<HTMLElement>(Boolean(pendingAction))
  const act=async(action:CandidateAction)=>{ if(busy)return; setBusy(`preflight:${action}`); setFeedback(undefined); try { const pre=await api.preflight(value.id,action); setActionNote(action==='review'?'【评分复核】':''); setActionReason('other'); setPendingAction({action,token:pre.token,impact:pre.impact,expires_at:pre.expires_at}) } catch(e) { setFeedback({tone:'error',text:copilotText(e)||'操作预检失败，请重试。'}) } finally { setBusy('') } }
  // stop 动作附 R10 原因枚举（默认 other）；其余动作保持原四参提交，契约字面量锚定不变。
  // 推荐：候选动作与顾问确认分别完成预检；动作 commit 成功后写入理由与确认时间。记录失败时保持对话框
  // 打开可重试——commit 走既有幂等重放，不回退、不误报成功；确认卡只在决定记录成功后出现。
  const commitAction=async()=>{
    if(!pendingAction||busy)return
    const {action,token}=pendingAction
    const reason=actionNote.trim()
    if(action==='recommend'&&!reason){setFeedback({tone:'error',text:'请先填写推荐理由，再确认推荐。'});return}
    setBusy(`commit:${action}`)
    setFeedback(undefined)
    try {
      const result=action==='stop'?await api.commit(value.id, action, token, actionNote.trim(), actionReason):await api.commit(value.id,action,token,actionNote.trim())
      // 通知名单弹窗等跨组件视图实时刷新，避免操作后列表状态滞后。
      dispatchCandidateUpdated({ id: value.id, stage: result.stage, isStopped: action==='stop' })
      // 同步通知 ASA 名单弹窗（/asa-floating 与 /asa-app 均通过同一服务端轮询刷新）。
      if (value.job_id) {
        try {
          await api.notifyFloatingCandidateUpdate(value.job_id, {
            job_candidate_id: value.id,
            stage: result.stage,
            is_stopped: action === 'stop',
          })
        } catch {
          // 通知失败不影响本地反馈，名单弹窗可依赖自身刷新兜底
        }
      }
      if(action==='recommend'){
        try {
          const decision=await recordRecommendationDecision(value.id,reason)
          setRecommendDecision({reason,decided_at:decision.decided_at||new Date().toISOString()})
          setPendingAction(undefined)
          const repeated=result.already_applied||result.receipt?.idempotent_replay||decision.already_applied
          setFeedback({tone:'success',text:repeated?`${candidateActionLabels[action]}此前已完成，已同步当前候选人状态${result.stage?`（${result.stage}）`:''}，推荐理由已确认记录。${recommendationPackageNote(decision.package)}`:`已推荐已完成，候选人状态已更新，推荐理由与确认时间已记录。${recommendationPackageNote(decision.package)}`})
        } catch(e) {
          setFeedback({tone:'error',text:`已推荐状态已更新，但推荐理由确认记录失败：${copilotText(e)||'请稍后重试'}。对话框可重试，重复确认不会重复推荐。`})
        } finally {
          await Promise.resolve(changed()).catch(()=>undefined)
        }
      } else {
        setPendingAction(undefined)
        await changed()
        const repeated=result.already_applied||result.receipt?.idempotent_replay
        setFeedback({tone:'success',text:repeated?`${candidateActionLabels[action]}此前已完成，已同步当前候选人状态${result.stage?`（${result.stage}）`:''}。`:action==='review'?'评分复核结论已记录到候选人事件。':`${candidateActionLabels[action]}已完成，候选人状态已更新。`})
      }
    } catch(e) {
      await Promise.resolve(changed()).catch(()=>undefined)
      setFeedback({tone:'error',text:`${copilotText(e)||'操作提交失败，请重试。'} 已重新读取候选人状态。`})
    } finally {
      setBusy('')
    }
  }
  const resume=value.resume||{summary:'',full_text:'',work_text:'',project_text:'',education_text:'',raw:{}}
  const links=[...new Map((value.source_links||[]).filter(x=>x.source_url).map(link=>[sourceLinkLabel(link.source_system),link])).values()]
  const relations=value.job_relations||[]
  const events=value.events||[]
  const stage=value.clean_stage||''
  const reviewPassed=['S2 ','S3 ','S7 ','S8 ','S9 ','S10 ','S11 ','S12 ','S13 '].some(prefix=>stage.startsWith(prefix))
  const contacted=['S3 ','S7 ','S8 ','S9 ','S10 ','S11 ','S12 ','S13 '].some(prefix=>stage.startsWith(prefix))
  const recommended=['S7 ','S8 ','S9 ','S10 ','S11 ','S12 ','S13 '].some(prefix=>stage.startsWith(prefix))
  const reportArtifacts=value.report_artifacts||[]
  // 弹出为独立窗口：macOS 宿主 openDetachedDialog 打开可自由拖出屏幕的原生窗口。
  const detachPanel=(anchor?:DragResizeAnchor):boolean=>{
    if(nativeBridge('openDetachedDialog',{title:value.name,url:`/asa-app#candidate=${value.id}&bare=1`,anchor})){
      close()
      return true
    }
    return false
  }
  return <><DialogPanel panelClassName="detail-panel candidate-panel" onEscape={close} onDetach={detachPanel}><header className="detail-head candidate-head" style={{ cursor: 'grab', userSelect: 'none', touchAction: 'none' }} title="按住拖动；拖出屏幕边缘可弹出为独立窗口"><div className="candidate-head-primary"><button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ChevronLeft/></button><div><h2>{value.name}</h2><p>{value.current_title || '职位待补充'} · {value.current_company || '公司待补充'} · {value.city || '城市待补充'}</p></div></div><div className="detail-actions"><button className="button" onClick={()=>openAgentWorkspace({type:'candidate',id:value.id,candidate:value.name,client:value.client,job:value.job})}><MessageSquareText/>交给 Agent</button>{links.map(link=><a className="button" href={link.source_url} target="_blank" rel="noreferrer" title={`打开${sourceLinkLabel(link.source_system)}`} key={sourceLinkLabel(link.source_system)}>{sourceLinkLabel(link.source_system)}<ExternalLink/></a>)}{value.is_stopped?<span className="button danger"><Ban/>已停止推进{value.stop_reason_label?` · ${value.stop_reason_label}`:''}</span>:<><button className="button" disabled={!!busy} onClick={()=>act('review')}>{busy==='preflight:review'?<LoaderCircle className="spin"/>:<ClipboardCheck/>}评分复核</button><button className="button" disabled={!!busy||reviewPassed} onClick={()=>act('advance')}>{busy==='preflight:advance'?<LoaderCircle className="spin"/>:reviewPassed?<Check/>:null}复核通过</button><button className="button" disabled={!!busy||contacted} onClick={()=>act('contact')}>{busy==='preflight:contact'?<LoaderCircle className="spin"/>:contacted?<Check/>:null}已联系</button><button className="button primary" disabled={!!busy||recommended} onClick={()=>act('recommend')}>{busy==='preflight:recommend'?<LoaderCircle className="spin"/>:recommended?<Check/>:null}已推荐</button><button className="button danger" disabled={!!busy} onClick={()=>act('stop')}>{busy==='preflight:stop'?<LoaderCircle className="spin"/>:<Ban/>}停止</button></>}<button className="icon-btn candidate-dialog-detach" onClick={()=>void detachPanel()} title="弹出为独立窗口（可拖出屏幕）" aria-label="弹出为独立窗口"><ExternalLink/></button><button className="icon-btn" onClick={close} title="关闭" aria-label="关闭"><X/></button></div></header>
      {feedback&&<div className={`candidate-action-feedback ${feedback.tone}`} role={feedback.tone==='error'?'alert':'status'}>{feedback.tone==='error'?<TriangleAlert/>:<CircleCheck/>}<span>{feedback.text}</span></div>}
      {recommendDecision&&<RecommendationConfirmCard record={recommendDecision}/>}
      <div className="detail-body"><main><div className="candidate-main-content">
        <section className="resume-hero"><div><span>目标岗位</span><b>{value.client} · {value.job}</b></div><div><span>当前阶段</span><b>{value.clean_stage || value.flow_bucket || '待复核'}</b></div><div><span>经验 / 学历</span><b>{value.experience || '-'} · {value.education || '-'}</b></div></section>
        <nav className="candidate-tabs" aria-label="候选人详情"><button className={view==='overview'?'active':''} onClick={()=>setView('overview')}><UserRoundSearch/>概览</button><button className={view==='resume'?'active':''} onClick={()=>setView('resume')}><BriefcaseBusiness/>履历</button><button className={view==='activity'?'active':''} onClick={()=>setView('activity')}><Clock3/>记录</button><button className={view==='assessment'?'active':''} onClick={()=>setView('assessment')}><ClipboardCheck/>评估</button></nav>
        {view==='overview'&&<>
          <ResumeOverview text={resume.summary} candidate={value}/>
          {value.sourcing_attributions?.length>0&&<section className="sourcing-trace"><div className="sourcing-trace-head"><Search/><div><span>寻访来源</span><b>怎么找到他的</b></div></div>{value.sourcing_attributions.map(item=><div className="sourcing-trace-row" key={item.id}><div className="trace-main"><span>{sourceLabel(item.channel)} · {item.source_round||'寻访查询'}</span><b>{item.source_query}</b><small>{item.source_purpose||'根据岗位策略生成'}</small></div><div className="trace-side"><span className={`feedback-score ${formatFeedbackScore(item.learning_score).tone}`}>{formatFeedbackScore(item.learning_score).text}</span><LearningSignals item={item}/></div></div>)}</section>}
        </>}
        {view==='assessment'&&<CandidateAssessment candidateId={value.id} jobId={value.job_id}/>}
        {view==='resume'&&<div className="resume-workspace"><ResumeWorkDetail text={resume.full_text} fallback={<ResumeTimelineSection title="工作经历" text={resume.work_text} empty="尚未采集结构化工作经历，可通过来源链接核对原始简历。"/>}/><ResumeProjectSection text={resume.project_text}/><ResumeEducationSection text={resume.education_text}/>{resume.full_text&&<details className="raw-resume"><summary>完整原始履历</summary><pre>{resume.full_text}</pre></details>}</div>}
        {view==='activity'&&<div className="candidate-records"><section className="resume-section"><h3>岗位关系</h3><div className="relation-list">{relations.map(r=><div key={r.id}><div><b>{r.job}</b><span>{r.client}</span></div><small>{r.clean_stage || r.flow_bucket}</small></div>)}</div></section><section className="resume-section"><div className="lifecycle-head"><h3>业务时间线</h3><button type="button" className="button lifecycle-record-toggle" onClick={()=>setLifecycleFormOpen(open=>!open)}><CalendarPlus/>记录面试/Offer/入职</button></div>{lifecycleFormOpen&&<LifecycleEventForm candidateId={value.id} onRecorded={changed}/>}<div className="timeline timeline-main">{events.map(e=><div key={e.id}><i className={lifecycleEventTone(e.event_type)||undefined}/><span>{date(e.event_time)}</span><b>{e.summary || lifecycleEventLabel(e.event_type) || e.event_type}</b><small>{eventStatusLabel(e.event_status)}</small></div>)}</div></section></div>}
      </div></main><aside><SectionHead title="岗位关系" meta={`${relations.length} 条`} />{relations.map(r=><div className="aside-item" key={r.id}><b>{r.job}</b><span>{r.client}</span><small>{r.clean_stage || r.flow_bucket}</small></div>)}<SectionHead title="报告与产物" meta={`${reportArtifacts.length} 项`} />{reportArtifacts.length===0?<div className="aside-empty"><FileText/><span>尚未生成匹配或推荐报告</span></div>:reportArtifacts.map(artifact=><button type="button" className="aside-item artifact-item" key={artifact.artifact_id} onClick={()=>setArtifactCardId(artifact.artifact_id)} aria-label={`查看人选产物：${artifact.title}`}><FileText/><span><b>{artifact.title}</b><small>{artifactTypeLabel(artifact.artifact_type)} v{artifact.version} · {artifactStatusLabel(artifact.validation_status)}</small></span></button>)}<RecommendationPackagesSection packages={value.recommendation_packages||[]}/><SectionHead title="最近动态" meta={`${events.length} 条`} /><div className="timeline">{events.slice(0,8).map(e=><div key={e.id}><i className={lifecycleEventTone(e.event_type)||undefined}/><span>{date(e.event_time)}</span><b>{e.summary || lifecycleEventLabel(e.event_type) || e.event_type}</b><small>{eventStatusLabel(e.event_status)}</small></div>)}</div></aside></div>
    {artifactCardId&&<WorkflowArtifactDialog key={artifactCardId} artifactId={artifactCardId} onClose={()=>setArtifactCardId('')}/>}</DialogPanel>{pendingAction&&<div className="action-dialog-backdrop" role="presentation"><section className="action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="candidate-action-title"><header><span className={`action-dialog-icon ${pendingAction.action==='stop'?'danger':''}`}>{pendingAction.action==='stop'?<Ban/>:<ShieldCheck/>}</span><div><small>操作确认</small><h3 id="candidate-action-title">{candidateActionLabels[pendingAction.action]}</h3></div><button className="icon-btn" disabled={busy.startsWith('commit:')} onClick={()=>{setPendingAction(undefined);setFeedback(undefined)}} title="取消" aria-label="取消"><X/></button></header><div className="action-dialog-body"><dl><div><dt>候选人</dt><dd>{value.name}</dd></div><div><dt>当前阶段</dt><dd>{value.clean_stage||value.flow_bucket||'待复核'}</dd></div></dl><p><ShieldCheck/>预检已通过：{pendingAction.impact}</p>{pendingAction.action==='stop'&&<><label><span>停止原因</span><select value={actionReason} onChange={event=>setActionReason(event.target.value)}>{stopReasonOptions.map(option=><option key={option.code} value={option.code}>{option.label}</option>)}</select></label><label><span>备注（选填）</span><textarea value={actionNote} onChange={event=>setActionNote(event.target.value)} placeholder="补充说明（选填）" rows={3}/></label></>}{pendingAction.action==='review'&&<><div className="review-conclusion-field"><span>复核结论（是尺严还是人不行）</span><div className="review-conclusion-chips"><button type="button" className="button" onClick={()=>setActionNote('【评分复核】结论：尺太严，建议放宽尺度')}>尺太严</button><button type="button" className="button" onClick={()=>setActionNote('【评分复核】结论：人不行，维持原判')}>人不行</button></div></div><label><span>结论备注</span><textarea value={actionNote} onChange={event=>setActionNote(event.target.value)} placeholder="【评分复核】尺太严还是人不行？写下判断依据…" rows={3}/></label></>}{pendingAction.action==='recommend'&&<RecommendationDecisionFields candidate={value} reason={actionNote} onReason={setActionNote}/>}{feedback?.tone==='error'&&<div className="action-dialog-error"><TriangleAlert/>{feedback.text}</div>}</div><footer><button className="button" disabled={busy.startsWith('commit:')} onClick={()=>{setPendingAction(undefined);setFeedback(undefined)}}>取消</button><button className={`button ${pendingAction.action==='stop'?'danger-fill':'primary'}`} disabled={busy.startsWith('commit:')||(pendingAction.action==='recommend'&&!actionNote.trim())} onClick={commitAction} data-dialog-initial-focus>{busy.startsWith('commit:')?<LoaderCircle className="spin"/>:pendingAction.action==='stop'?<Ban/>:<Check/>}确认{candidateActionLabels[pendingAction.action]}</button></footer></section></div>}</>
}

function ResumeOverview({text,candidate}:{text?:string;candidate:CandidateDetail}) {
  const overview=buildResumeOverview(text||'',{name:candidate.name,currentCompany:candidate.current_company,currentTitle:candidate.current_title,city:candidate.city,education:candidate.education,experience:candidate.experience})
  const hasContent=overview.fields.length||overview.intent||overview.tags.length||overview.fallback
  if(!hasContent)return <section className="resume-section"><h3>职业概览</h3><div className="empty">暂无职业概览。</div></section>
  return <section className="resume-section resume-overview"><h3>职业概览</h3>{overview.fields.length>0&&<dl>{overview.fields.map(row=><div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>}{overview.fallback&&<p className="overview-basic">{overview.fallback}</p>}{overview.intent&&<div className="overview-intent"><span>求职意向</span><b>{overview.intent}</b></div>}{overview.tags.length>0&&<div className="overview-keywords"><span>意向补充</span><div>{overview.tags.map(tag=><i key={tag}>{tag}</i>)}</div></div>}</section>
}

function ResumeWorkDetail({text,fallback}:{text?:string;fallback:React.ReactNode}) {
  const blocks=parseWorkDetails(text||'')
  if(!blocks.length)return <>{fallback}</>
  return <section className="resume-section"><h3>工作经历<span>{blocks.length} 段 · 含职责详情</span></h3><div className="resume-timeline">{blocks.map(block=><div key={`${block.company}-${block.period}`}><i/><div><b>{block.company}</b><div className="resume-timeline-entries"><span>{[block.role,block.period].filter(Boolean).join(' · ')}</span></div><ul className="work-duty">{block.description.map((line,i)=><li key={i}>{line}</li>)}</ul></div></div>)}</div></section>
}

function ResumeTimelineSection({title,text,empty}:{title:string;text?:string;empty:string}) {
  const items=String(text||'').split(/\n+/).map(item=>item.trim()).filter(Boolean)
  const groups=items.reduce<Array<{label:string;entries:string[]}>>((result,item)=>{const parts=item.split(/\s*·\s*/).map(part=>part.trim()).filter(Boolean);const label=parts[0]||item;const entry=parts.slice(1).join(' · ');const existing=result.find(group=>group.label===label);if(existing)existing.entries.push(entry);else result.push({label,entries:[entry]});return result},[])
  return <section className="resume-section"><h3>{title}<span>{items.length?`${groups.length} 组 · ${items.length} 段`:''}</span></h3>{groups.length?<div className="resume-timeline">{groups.map(group=><div key={group.label}><i/><div><b>{group.label}</b><div className="resume-timeline-entries">{group.entries.filter(Boolean).map((entry,index)=><span key={`${entry}-${index}`}>{entry}</span>)}</div></div></div>)}</div>:<div className="empty">{empty}</div>}</section>
}

function ResumeProjectSection({text}:{text?:string}) {
  const projects=parseProjectDetails(text||'')
  if(!projects.length)return <ResumeSourceFallback title="项目经历" text={text} empty="暂无结构化项目经历。"/>
  return <section className="resume-section"><h3>项目经历<span>{projects.length} 段</span></h3><div className="project-history">{projects.map((project,index)=><article key={`${project.title}-${project.period}`}><header><div><b>{project.title}</b><span>{project.period}</span></div></header>{(project.role||project.company)&&<dl className="history-meta">{project.role&&<div><dt>项目职务</dt><dd>{project.role}</dd></div>}{project.company&&<div><dt>所在公司</dt><dd>{project.company}</dd></div>}</dl>}{project.description[0]&&<p className="project-preview">{project.description[0]}</p>}{(project.description.length>1||project.duties.length||project.achievements.length)&&<details open={index===0}><summary>项目详情</summary><ResumeHistoryField label="项目描述" lines={project.description.slice(1)}/><ResumeHistoryField label="项目职责" lines={project.duties}/><ResumeHistoryField label="项目业绩" lines={project.achievements}/></details>}</article>)}</div></section>
}

function ResumeEducationSection({text}:{text?:string}) {
  const education=parseEducationDetails(text||'')
  if(!education.length)return <ResumeSourceFallback title="教育经历" text={text} empty="暂无结构化教育经历。"/>
  return <section className="resume-section"><h3>教育经历<span>{education.length} 段</span></h3><div className="education-history">{education.map(item=><article key={`${item.school}-${item.period}`}><div><b>{item.school}</b><span>{item.period}</span></div>{(item.degree||item.major)&&<p>{[item.degree,item.major].filter(Boolean).join(' · ')}</p>}{item.details.length>0&&<small>{item.details.map(detail=><i key={detail}>{detail}</i>)}</small>}</article>)}</div></section>
}

function ResumeHistoryField({label,lines}:{label:string;lines:string[]}) {
  if(!lines.length)return null
  return <section className="history-detail"><span>{label}</span><div>{lines.map((line,index)=><p key={`${line}-${index}`}>{line}</p>)}</div></section>
}

// 未知来源格式保留原文，但不再复用逐行时间轴，防止单条履历被拆成数百个空节点。
function ResumeSourceFallback({title,text,empty}:{title:string;text?:string;empty:string}) {
  const source=String(text||'').trim()
  return <section className="resume-section"><h3>{title}</h3>{source?<details className="resume-source-fallback"><summary>查看原始{title}</summary><pre>{source}</pre></details>:<div className="empty">{empty}</div>}</section>
}

function LearningSignals({item}:{item:CandidateDetail['sourcing_attributions'][number]}) {
  const signals=[['通过',item.review_pass_count],['联系',item.contacted_count],['推荐',item.recommended_count],['停止',item.stopped_count],['客户正向',item.client_positive_count],['客户否决',item.client_rejected_count]].filter(([,count])=>Number(count||0)>0)
  return <div className="learning-signals">{signals.length?signals.map(([label,count])=><span key={String(label)}>{label} {Number(count)}</span>):<span>暂无后续业务反馈</span>}</div>
}
