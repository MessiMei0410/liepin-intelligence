import { ChevronDown, Route } from 'lucide-react'
import { recordValue, arrayValue } from '../shared/records'
import { StrategyCoverage } from './StrategyCoverage'

export function WorkflowStrategy({strategy,channels,gates,coverage,highlights,open,toggle}:{strategy:Record<string,unknown>;channels:Record<string,unknown>;gates:Record<string,unknown>;coverage?:unknown;highlights?:string[];open:boolean;toggle:()=>void}) {
  const liepin=arrayValue(channels.liepin)
  const xsaas=arrayValue(channels.xsaas)
  const hasStrategy=liepin.length+xsaas.length>0
  const generation=recordValue(strategy.generation)
  const flashTerms=(highlights||[]).filter(term=>term.trim())
  // 顾问修订约束（capability_runtime._lock_consultant_constraints 锁定的用户原话硬条件）：
  // 修订链生效后左侧面板必须可见，不随"查看完整策略"折叠。
  const constraints=arrayValue(strategy.consultant_constraints).map(recordValue).filter(item=>String(item.rule||'').trim())
  const renderQueries=(items:unknown[],label:string)=>{
    const visible=open?items:items.slice(0,3)
    return <div className="strategy-channel"><div className="strategy-channel-head"><b>{label}</b><span>{items.length} 组关键词</span></div><div className="strategy-queries">{visible.map((entry,index)=>{const item=recordValue(entry);const flash=flashTerms.some(term=>String(item.query||'').includes(term));return <div key={`${item.query}-${index}`} className={flash?'flash-new':undefined}><b>{String(item.query||'关键词待补充')}</b><span>{String(item.purpose||'按岗位画像检索')}</span></div>})}</div>{!open&&items.length>3&&<small>另有 {items.length-3} 组关键词</small>}</div>
  }
  return <section className="workflow-insight workflow-strategy">
    <header><span className="insight-icon"><Route/></span><div><span>多渠道寻访策略</span><b>{hasStrategy?`猎聘 ${liepin.length} 组 · X-SaaS ${xsaas.length} 组`:'等待生成策略'}</b>{generation.mode&&<small>{generation.mode==='llm'?`大模型生成 · ${generation.model||'ASA Model'}`:'规则兜底'}{Number(generation.memory_hits||0)>0?` · 参考 ${generation.memory_hits} 条岗位记忆`:''}{Number(generation.experiment_count||0)>0?` · ${generation.experiment_count} 条历史实验`:''}</small>}</div>{hasStrategy&&<button className="button" onClick={toggle} aria-expanded={open}>{open?'收起策略':'查看完整策略'}<ChevronDown className={open?'rotate':''}/></button>}</header>
    {constraints.length>0&&<div className="strategy-constraints"><b>顾问修订约束</b><div>{constraints.map((item,index)=>{const meta=constraintMeta(item.type);return <span key={index} className={meta.tone}><i>{meta.label}</i>{String(item.rule)}</span>})}</div></div>}
    {hasStrategy?<><div className="strategy-grid">{renderQueries(liepin,'猎聘')}{renderQueries(xsaas,'X-SaaS')}</div>{open&&<div className="strategy-rules"><StrategyRule title="硬性条件" values={arrayValue(gates.hard_requirements)} tone="required"/><StrategyRule title="排除规则" values={arrayValue(gates.negative_rules)} tone="excluded"/><StrategyRule title="风险提醒" values={arrayValue(gates.risk_points)} tone="risk"/></div>}</>:<div className="insight-empty">完成“生成多渠道寻访策略”后，这里会展示每个渠道的关键词与筛选规则。</div>}
    <StrategyCoverage report={coverage}/>
  </section>
}

function constraintMeta(type:unknown):{label:string;tone:string} {
  if(type==='hard_requirement')return {label:'硬性',tone:'hard'}
  if(type==='preference')return {label:'优先',tone:'prefer'}
  if(type==='conditional_acceptance')return {label:'有条件',tone:'cond'}
  return {label:'约束',tone:''}
}

function StrategyRule({title,values,tone}:{title:string;values:unknown[];tone:string}) {
  if(!values.length)return null
  return <div className={`strategy-rule ${tone}`}><b>{title}</b><div>{values.map((value,index)=><span key={index}>{String(value)}</span>)}</div></div>
}
