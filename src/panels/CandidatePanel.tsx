import { useState } from 'react'
import { Ban, BriefcaseBusiness, Check, ChevronLeft, CircleCheck, Clock3, ExternalLink, LoaderCircle, MessageSquareText, Search, ShieldCheck, TriangleAlert, UserRoundSearch, X } from 'lucide-react'
import { api, CandidateDetail } from '../api'
import { date, sourceLabel, sourceLinkLabel, eventStatusLabel } from '../shared/format'
import { copilotText } from '../shared/text'
import { SectionHead } from '../shared/primitives'
import { openCopilotWindow } from '../copilot/bridge'

export type CandidateAction = 'advance' | 'contact' | 'recommend' | 'stop'
export type CandidateActionPreflight = { action: CandidateAction; token: string; impact: string; expires_at?: string }
const candidateActionLabels: Record<CandidateAction,string> = {
  advance:'复核通过', contact:'标记已联系', recommend:'标记已推荐', stop:'停止推进',
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
