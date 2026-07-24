import { ChevronLeft, MessageSquareText, X, ShieldAlert, Route, UserRoundSearch, ChevronRight, CircleCheck, CircleDashed, Check } from 'lucide-react'
import { JobDetail } from '../api'
import { JobProfileInsights } from './JobProfileInsights'
import { recordValue, textList } from '../shared/records'
import { date, sourceLabel } from '../shared/format'
import { SectionHead } from '../shared/primitives'
import { openCopilotWindow } from '../copilot/bridge'

export function JobPanel({value,close,openCandidate}:{value:JobDetail,close:()=>void,openCandidate:(id:number)=>void}) {
  const position=recordValue(value.position)
  const profile=recordValue(value.profile)
  const hard=textList(profile.hard_requirements,position.hard_requirements,value.hard_requirements)
  const abilities=textList(profile.ability_keywords,position.ability_keywords,value.ability_keywords)
  const targets=textList(profile.target_companies,position.target_companies,value.target_companies,value.metric_target_companies)
  const exclusions=textList(profile.exclusion_tags,position.exclusions,value.exclusions,value.exclude_terms)
  const keywords=textList(profile.search_keywords,position.search_words,value.search_words,value.next_keywords)
  const pitch=textList(profile.pitch_points)
  const risks=textList(profile.risk_points,value.risk)
  const maxStage=Math.max(1,...value.stages.map(item=>item.count))
  const facts=[
    ['优先级',value.priority||'常规'],['状态',value.status||value.lifecycle_stage||'待启动'],
    ['地点',position.location||value.location||'待确认'],['薪资',position.salary||'待确认'],
    ['编制',position.headcount??'待确认'],['截止',position.deadline||'待确认'],
    ['学历',position.education||profile.education_requirement||'待确认'],['经验',position.experience||profile.experience_requirement||'待确认'],
  ]
  return <div className="overlay"><article className="detail-panel job-detail-panel"><header className="detail-head"><button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ChevronLeft/></button><div><h2>{value.title}</h2><p>{value.client} · {position.department||position.team||value.location||'岗位详情'} · 岗位 #{value.id}</p></div><div className="detail-actions"><button className="button" onClick={()=>openCopilotWindow({type:'job',id:value.id,client:value.client,job:value.title})}><MessageSquareText/>Copilot</button>{value.priority?.includes('P0')&&<span className="tag warn">P0 最急</span>}<button className="icon-btn" onClick={close} title="关闭" aria-label="关闭"><X/></button></div></header><div className="job-detail-body"><main>
    <section className="job-funnel" aria-label="岗位漏斗"><div><span>全部人选</span><b>{value.funnel.total}</b></div><div><span>活跃推进</span><b>{value.funnel.active}</b></div><div><span>已触达</span><b>{value.funnel.contacted}</b></div><div><span>已推荐</span><b>{value.funnel.recommended}</b></div><div><span>已停止</span><b>{value.funnel.stopped}</b></div></section>
    <section className="job-detail-section"><h3>岗位概况</h3><div className="job-facts">{facts.map(([label,content])=><div key={label}><span>{label}</span><b>{String(content)}</b></div>)}</div>{(profile.jd_analysis_summary||value.summary)&&<p className="job-summary">{String(profile.jd_analysis_summary||value.summary)}</p>}{value.closed_reason&&<p className="job-warning">{value.closed_reason}</p>}</section>
    <JobProfileInsights key={value.id} jobId={value.id}/>
    <JobListSection title="硬性要求" items={hard}/>
    <JobListSection title="核心能力" items={abilities}/>
    <JobListSection title="岗位卖点" items={pitch}/>
    <div className="job-detail-columns"><JobListSection title="目标公司" items={targets}/><JobListSection title="排除条件" items={exclusions} tone="warn"/></div>
    <section className="job-detail-section"><h3>阶段分布</h3>{value.stages.length?<div className="job-stage-list">{value.stages.map(item=><div key={item.stage}><span>{item.stage}</span><i><b style={{width:`${Math.max(5,item.count/maxStage*100)}%`}}/></i><strong>{item.count}</strong></div>)}</div>:<div className="empty">当前岗位还没有候选人阶段记录。</div>}</section>
    <section className="job-detail-section"><h3>寻访策略</h3>{keywords.length?<div className="job-keywords">{keywords.map(item=><span key={item}>{item}</span>)}</div>:<div className="empty">暂无已确认的寻访关键词。</div>}{risks.length>0&&<div className="job-risk-list">{risks.map(item=><p key={item}><ShieldAlert/>{item}</p>)}</div>}{value.search_experiments.length>0&&<div className="job-experiments">{value.search_experiments.slice(0,12).map(item=><div key={item.id}><Route/><div><b>{String(item.query||'未记录关键词')}</b><span>{sourceLabel(String(item.channel||''))} · 结果 {Number(item.result_count||0)} · 提取 {Number(item.extracted_count||0)} · 推荐 {Number(item.recommended_count||0)}</span></div><small>{date(String(item.updated_at||item.run_time||''))}</small></div>)}</div>}</section>
  </main><aside><SectionHead title="岗位人选" meta={`${value.candidates.length} 人`} />{value.candidates.length===0?<div className="aside-empty"><UserRoundSearch/><span>当前岗位还没有人选</span></div>:<div className="job-candidate-list">{value.candidates.map(candidate=><button key={candidate.id} onClick={()=>openCandidate(candidate.id)}><UserRoundSearch/><div><b>{candidate.name}</b><span>{candidate.current_company||'公司待补充'} · {candidate.current_title||'职位待补充'}</span><small>{candidate.clean_stage||candidate.flow_bucket||'待复核'}</small></div><ChevronRight/></button>)}</div>}
    <SectionHead title="待办" meta={`${value.followups.length} 条`} />{value.followups.length===0?<div className="aside-empty"><CircleCheck/><span>当前没有岗位待办</span></div>:value.followups.slice(0,12).map(item=><div className="aside-item" key={item.id}><b>{String(item.candidate_name||item.task_type||'岗位待办')}</b><span>{String(item.reason||'待处理')}</span><small>{item.due_at?`截止 ${date(String(item.due_at))}`:'未设置截止时间'}</small></div>)}
    <SectionHead title="最近动态" meta={`${value.events.length} 条`} />{value.events.length===0?<div className="aside-empty"><CircleDashed/><span>当前没有业务动态</span></div>:<div className="timeline">{value.events.slice(0,20).map(event=><div key={event.id}><i/><span>{date(event.event_time)}</span><b>{event.summary||event.event_type}</b><small>{event.event_status}</small></div>)}</div>}</aside></div></article></div>
}

export function JobListSection({title,items,tone=''}:{title:string;items:string[];tone?:string}) {
  if(!items.length)return null
  return <section className={`job-detail-section ${tone}`}><h3>{title}</h3><div className="job-list-items">{items.map(item=><div key={item}><Check/><span>{item}</span></div>)}</div></section>
}
