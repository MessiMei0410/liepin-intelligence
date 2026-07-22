import { BriefcaseBusiness, Building2, ChevronRight, MapPin, Target } from 'lucide-react'

export function WorkflowTarget({target,objective}:{target:{client:string;job:string;location:string;status:string;priority:string;id:number},objective:string}) {
  const openJob=()=>{if(target.id)location.hash=`job=${target.id}`}
  return <section className="workflow-insight workflow-target">
    <header><span className="insight-icon"><Target/></span><div><span>本次对应岗位</span><b>{target.client} / {target.job}</b></div>{target.id>0&&<button className="button" onClick={openJob}><BriefcaseBusiness/>打开岗位<ChevronRight/></button>}</header>
    <div className="target-facts"><div><Building2/><span>客户</span><b>{target.client}</b></div><div><BriefcaseBusiness/><span>岗位</span><b>{target.job}</b></div><div><MapPin/><span>状态</span><b>{target.location||'地点待确认'}{target.status?` · ${target.status}`:''}</b></div></div>
    <p>{objective}</p>{target.priority?.includes('P0')&&<span className="tag warn">P0 最急</span>}
  </section>
}
