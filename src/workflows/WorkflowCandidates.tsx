import React, { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, LoaderCircle, UserRoundSearch, UsersRound } from 'lucide-react'
import { api } from '../api'
import { WorkflowCandidateItem } from '../workflow/workflowSummary'
import { sourceLabel } from '../shared/format'

// R7：人选结果改走 /candidates 分页路由（摘要字段，total 在响应里），不再依赖详情大对象的 assessed_items。
// 首开/切换工作流拉第一页，"加载更多"增量翻页；父面板按需重载详情（updatedAt 变化）时静默刷新已加载窗口。
export function WorkflowCandidates({workflowId,updatedAt,sourcingStatus,openCandidate,ref}:{workflowId:string;updatedAt:string;sourcingStatus:string;openCandidate:(id:number)=>void;ref?:React.Ref<HTMLElement>}) {
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
