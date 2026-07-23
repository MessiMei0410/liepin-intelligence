import { useMemo } from 'react'
import { UserRoundSearch } from 'lucide-react'
import { Candidate } from '../api'
import { candidateStopped } from '../shared/format'

export function Progress({items,openCandidate}:{items:Candidate[],openCandidate:(id:number)=>void}) {
  const groups = useMemo(() => Object.entries(items.reduce<Record<string,Candidate[]>>((a,c) => { const k=c.flow_bucket || c.clean_stage || '待复核'; (a[k] ||= []).push(c); return a },{})).sort((a,b)=>Number(candidateStopped(a[1][0]))-Number(candidateStopped(b[1][0]))),[items])
  return groups.length?<div className="stage-grid">{groups.map(([stage,list]) => <section className="stage" key={stage}><header><span>{stage}</span><b>{list.length}</b></header>{list.map(c => <button key={c.id} onClick={() => openCandidate(c.id)}><UserRoundSearch/><span><b>{c.name}</b><small>{c.client} · {c.job}</small></span></button>)}</section>)}</div>:<div className="empty">没有符合当前条件的人选进度。</div>
}
