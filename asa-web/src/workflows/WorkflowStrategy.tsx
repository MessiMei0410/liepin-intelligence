import { useState } from 'react'
import { ChevronDown, Pencil, Route, Trash2 } from 'lucide-react'
import { api } from '../api'
import type { StrategyItemEdit } from '../api'
import { recordValue, arrayValue } from '../shared/records'
import { StrategyCoverage } from './StrategyCoverage'

type WorkflowStrategyProps = {
  strategy:Record<string,unknown>;channels:Record<string,unknown>;gates:Record<string,unknown>
  coverage?:unknown;highlights?:string[];open:boolean;toggle:()=>void
  // 按项编辑一期：strategy_v2（step4 组级 targets / step5 预期召回）+ 编辑入口。
  // editable=false 或缺 workflowId 时纯展示；提交走 /strategy/edits，失败保留原数据并显示错误。
  strategyV2?:Record<string,unknown>;workflowId?:string;editable?:boolean;onEdited?:()=>void|Promise<void>
}

export function WorkflowStrategy({strategy,channels,gates,coverage,highlights,open,toggle,strategyV2,workflowId,editable,onEdited}:WorkflowStrategyProps) {
  const liepin=arrayValue(channels.liepin)
  const xsaas=arrayValue(channels.xsaas)
  const hasStrategy=liepin.length+xsaas.length>0
  const generation=recordValue(strategy.generation)
  const flashTerms=(highlights||[]).filter(term=>term.trim())
  // 顾问修订约束（capability_runtime._lock_consultant_constraints 锁定的用户原话硬条件）：
  // 修订链生效后左侧面板必须可见，不随"查看完整策略"折叠。
  const constraints=arrayValue(strategy.consultant_constraints).map(recordValue).filter(item=>String(item.rule||'').trim())
  const v2=strategyV2||{}
  const groups=arrayValue(v2.step4_keyword_groups).map(recordValue).filter(item=>String(item.group||'').trim())
  const pools=arrayValue(v2.step2_target_pool).map(recordValue).filter(item=>arrayValue(item.companies).length>0)
  const levels=arrayValue(recordValue(v2.step3_level_mapping).accepted_levels).map(String).filter(item=>item.trim())
  const expectation=recordValue(v2.step5_expectation)
  const expectedRecall=Object.entries(recordValue(expectation.expected_recall_per_tier))
  const fallbackPlan=String(expectation.fallback_plan||'').trim()
  const consultantJudgement=recordValue(v2.consultant_judgement)
  const diagnosis=recordValue(consultantJudgement.role_diagnosis)
  const marketView=recordValue(consultantJudgement.market_view)
  const evidenceStandard=recordValue(consultantJudgement.evidence_standard)
  const clientCalibration=recordValue(consultantJudgement.client_calibration)
  const searchSequence=arrayValue(consultantJudgement.search_sequence).map(recordValue)
  const expansionLadder=arrayValue(consultantJudgement.expansion_ladder).map(recordValue)
  const mustVerify=arrayValue(evidenceStandard.must_verify).map(String).filter(item=>item.trim())
  const mustConfirm=arrayValue(clientCalibration.must_confirm).map(String).filter(item=>item.trim())
  const judgementBasis=arrayValue(consultantJudgement.basis).map(String).filter(item=>item.trim())
  const hasConsultantJudgement=Object.keys(consultantJudgement).length>0
  const hasV2=groups.length+pools.length+levels.length+expectedRecall.length+(hasConsultantJudgement?1:0)>0
  const canEdit=Boolean(editable&&workflowId)

  const [busyEdit,setBusyEdit]=useState(false)
  const [editError,setEditError]=useState('')
  const [receipt,setReceipt]=useState('')
  const [editingGroup,setEditingGroup]=useState('')
  const [formTerms,setFormTerms]=useState('')
  const [formTargets,setFormTargets]=useState('')
  const [confirmDelete,setConfirmDelete]=useState('')
  const [editingLevels,setEditingLevels]=useState(false)
  const [levelsText,setLevelsText]=useState('')

  const submitEdits=async(edits:StrategyItemEdit[])=>{
    if(!workflowId||busyEdit)return
    setBusyEdit(true);setEditError('');setReceipt('')
    try{
      const preflight=await api.preflightStrategyEdits(workflowId,edits)
      const result=await api.applyStrategyEdits(workflowId,edits,'',preflight.strategy_hash,preflight.preflight_token)
      const applied=(result.applied||[]).map(item=>String(item.summary||'')).filter(Boolean)
      setReceipt(`已保存为策略 revision ${result.revision??'-'}：${applied.join('；')||'编辑已生效'}${result.approval_refreshed?'；寻访审批卡已换新，需重新批准':''}`)
      setEditingGroup('');setConfirmDelete('');setEditingLevels(false)
      if(onEdited)await onEdited()
    }catch(error){
      // 失败保留原数据：不改本地展示，只显示后端可读错误（409 中文 detail）。
      setEditError(error instanceof Error?error.message:'策略保存失败，请重试')
    }finally{setBusyEdit(false)}
  }
  const startEditGroup=(name:string)=>{
    const group=groups.find(item=>String(item.group||'')===name)
    setEditingGroup(name);setConfirmDelete('')
    setFormTerms(arrayValue(group?.terms).map(String).join('\n'))
    setFormTargets(String(group?.targets||''))
  }
  const saveGroup=(name:string)=>{
    const terms=formTerms.split(/[\n、；;，,]+/).map(term=>term.trim()).filter(Boolean)
    submitEdits([{op:'update_keyword_group',group:name,terms,targets:formTargets.trim()}])
  }
  const saveLevels=()=>{
    const acceptedLevels=levelsText.split(/[\n、；;，,/]+/).map(item=>item.trim()).filter(Boolean)
    submitEdits([{op:'update_accepted_levels',accepted_levels:acceptedLevels}])
  }

  const renderQueries=(items:unknown[],label:string)=>{
    const visible=open?items:items.slice(0,3)
    return <div className="strategy-channel"><div className="strategy-channel-head"><b>{label}</b><span>{items.length} 组关键词</span></div><div className="strategy-queries">{visible.map((entry,index)=>{const item=recordValue(entry);const flash=flashTerms.some(term=>String(item.query||'').includes(term));return <div key={`${item.query}-${index}`} className={flash?'flash-new':undefined}><b>{String(item.query||'关键词待补充')}</b><span>{String(item.purpose||'按岗位画像检索')}</span></div>})}</div>{!open&&items.length>3&&<small>另有 {items.length-3} 组关键词</small>}</div>
  }
  return <section className="workflow-insight workflow-strategy">
    <header><span className="insight-icon"><Route/></span><div><span>多渠道寻访策略</span><b>{hasStrategy?`猎聘 ${liepin.length} 组 · X-SaaS ${xsaas.length} 组`:'等待生成策略'}</b>{Boolean(generation.mode)&&<small>{generation.mode==='llm'?`大模型生成 · ${String(generation.model||'ASA Model')}`:'规则兜底'}{Number(generation.memory_hits||0)>0?` · 参考 ${generation.memory_hits} 条岗位记忆`:''}{Number(generation.experiment_count||0)>0?` · ${generation.experiment_count} 条历史实验`:''}</small>}</div>{hasStrategy&&<button className="button" onClick={toggle} aria-expanded={open}>{open?'收起策略':'查看完整策略'}<ChevronDown className={open?'rotate':''}/></button>}</header>
    {constraints.length>0&&<div className="strategy-constraints"><b>顾问修订约束</b><div>{constraints.map((item,index)=>{const meta=constraintMeta(item.type);return <span key={index} className={meta.tone}><i>{meta.label}</i>{String(item.rule)}</span>})}</div></div>}
    {hasStrategy?<><div className="strategy-grid">{renderQueries(liepin,'猎聘')}{renderQueries(xsaas,'X-SaaS')}</div>{open&&<div className="strategy-rules"><StrategyRule title="硬性条件" values={arrayValue(gates.hard_requirements)} tone="required"/><StrategyRule title="排除规则" values={arrayValue(gates.negative_rules)} tone="excluded"/><StrategyRule title="风险提醒" values={arrayValue(gates.risk_points)} tone="risk"/></div>}</>:<div className="insight-empty">完成“生成多渠道寻访策略”后，这里会展示每个渠道的关键词与筛选规则。</div>}
    {open&&hasV2&&<div className="strategy-v2-detail">
      {(receipt||editError)&&<div className={editError?'strategy-edit-error':'strategy-edit-receipt'} role={editError?'alert':'status'} aria-live="polite">{editError||receipt}</div>}
      {hasConsultantJudgement&&<section className="strategy-consultant-brief" aria-label="资深顾问判断">
        <header><div><b>顾问判断</b><span>{String(diagnosis.role_family||'岗位族待核验')}{marketView.reason?` · ${String(marketView.reason)}`:''}</span></div>{judgementBasis.length>0&&<small>依据：{judgementBasis.join('、')}</small>}</header>
        <div className="strategy-consultant-core">
          <div><b>岗位本质</b><p>{String(diagnosis.business_mandate||'岗位本质待补充')}</p></div>
          <div><b>候选人原型</b><p>{String(diagnosis.candidate_archetype||'候选人原型待补充')}</p></div>
        </div>
        <div className="strategy-consultant-grid">
          {searchSequence.length>0&&<div><b>搜索顺序</b><ol>{searchSequence.slice(0,4).map((item,index)=><li key={`${item.round}-${index}`}><strong>{String(item.round||index+1)} · {String(item.name||'寻访')}</strong><span>{String(item.purpose||item.target||'')}</span></li>)}</ol></div>}
          {expansionLadder.length>0&&<div><b>扩池边界</b><ol>{expansionLadder.slice(0,4).map((item,index)=><li key={`${item.step}-${index}`}><strong>{String(item.step||index+1)} · {String(item.direction||'待确认')}</strong><span>{String(item.trigger||'')}{item.tradeoff?`；代价：${String(item.tradeoff)}`:''}</span></li>)}</ol></div>}
        </div>
        {(mustVerify.length>0||mustConfirm.length>0)&&<div className="strategy-consultant-checks">
          {mustVerify.length>0&&<div><b>人选核验</b><span>{mustVerify.slice(0,4).join('；')}</span></div>}
          {mustConfirm.length>0&&<div><b>客户校准</b><span>{mustConfirm.slice(0,4).join('；')}</span></div>}
        </div>}
      </section>}
      {groups.length>0&&<div className="strategy-groups"><b>关键词组画像</b>{groups.map((group,index)=>{
        const name=String(group.group||`group_${index+1}`)
        const targets=String(group.targets||'').trim()
        const terms=arrayValue(group.terms).map(String).filter(term=>term.trim())
        if(editingGroup===name)return <div key={name} className="strategy-group editing">
          <b>{name}</b>
          <label>目标画像<input value={formTargets} onChange={event=>setFormTargets(event.target.value)} aria-label={`关键词组 ${name} 的目标画像`}/></label>
          <label>词条（每行一条，单条 ≤2 个词）<textarea value={formTerms} onChange={event=>setFormTerms(event.target.value)} rows={Math.min(8,Math.max(3,formTerms.split('\n').length))} aria-label={`关键词组 ${name} 的词条`}/></label>
          <div className="strategy-edit-actions">
            <button type="button" className="button primary" disabled={busyEdit} onClick={()=>saveGroup(name)} aria-label={`保存关键词组 ${name}`}>保存该组</button>
            <button type="button" className="button" disabled={busyEdit} onClick={()=>setEditingGroup('')} aria-label={`取消编辑关键词组 ${name}`}>取消</button>
          </div>
        </div>
        return <div key={name} className="strategy-group">
          <div className="strategy-group-head"><b>{name}</b>{canEdit&&<span className="strategy-edit-actions">
            <button type="button" className="icon-btn" disabled={busyEdit} onClick={()=>startEditGroup(name)} aria-label={`编辑关键词组 ${name}`}><Pencil/></button>
            {confirmDelete===`group:${name}`
              ?<><button type="button" className="button danger" disabled={busyEdit} onClick={()=>submitEdits([{op:'delete_keyword_group',group:name}])} aria-label={`确认删除关键词组 ${name}`}>确认删除</button><button type="button" className="button" disabled={busyEdit} onClick={()=>setConfirmDelete('')} aria-label="取消删除">取消</button></>
              :<button type="button" className="icon-btn" disabled={busyEdit} onClick={()=>setConfirmDelete(`group:${name}`)} aria-label={`删除关键词组 ${name}`}><Trash2/></button>}
          </span>}</div>
          <span className="strategy-group-targets">目标画像：{targets||'未提供目标画像'}</span>
          {terms.length>0&&<div className="strategy-group-terms">{terms.map((term,termIndex)=><span key={termIndex}>{term}</span>)}</div>}
        </div>
      })}</div>}
      {pools.length>0&&<div className="strategy-pool"><b>目标公司池</b>{pools.map((pool,poolIndex)=>{
        const tier=String(pool.tier||`T${poolIndex+1}`)
        return <div key={`${tier}-${poolIndex}`} className="strategy-pool-tier"><span className="strategy-pool-tier-label">{tier}</span><div className="strategy-pool-companies">{arrayValue(pool.companies).map(recordValue).map((company,companyIndex)=>{
          const companyName=String(company.name||'').trim()
          if(!companyName)return null
          const key=`company:${tier}:${companyName}`
          return <span key={`${companyName}-${companyIndex}`} className="strategy-pool-company">{companyName}{canEdit&&(confirmDelete===key
            ?<><button type="button" className="button danger" disabled={busyEdit} onClick={()=>submitEdits([{op:'delete_company',tier,name:companyName}])} aria-label={`确认删除 ${tier} 池公司 ${companyName}`}>确认删除</button><button type="button" className="button" disabled={busyEdit} onClick={()=>setConfirmDelete('')} aria-label="取消删除">取消</button></>
            :<button type="button" className="icon-btn" disabled={busyEdit} onClick={()=>setConfirmDelete(key)} aria-label={`删除 ${tier} 池公司 ${companyName}`}><Trash2/></button>)}</span>
        })}</div></div>
      })}</div>}
      <div className="strategy-levels"><b>职级映射</b>{editingLevels
        ?<div className="strategy-edit-actions"><input value={levelsText} onChange={event=>setLevelsText(event.target.value)} placeholder="多个职级用顿号分隔" aria-label="可接受职级"/>
          <button type="button" className="button primary" disabled={busyEdit} onClick={saveLevels} aria-label="保存职级映射">保存职级</button>
          <button type="button" className="button" disabled={busyEdit} onClick={()=>setEditingLevels(false)} aria-label="取消编辑职级">取消</button></div>
        :<>{levels.length>0?<div className="strategy-group-terms">{levels.map((level,levelIndex)=><span key={levelIndex}>{level}</span>)}</div>:<span className="strategy-group-targets">未提供职级映射</span>}{canEdit&&<button type="button" className="icon-btn" disabled={busyEdit} onClick={()=>{setEditingLevels(true);setLevelsText(levels.join('、'))}} aria-label="修改职级映射"><Pencil/></button>}</>}
      </div>
      <div className="strategy-expectation"><b>预计质量 / 预期召回</b>{expectedRecall.length>0||fallbackPlan
        ?<div className="strategy-expectation-body">{expectedRecall.map(([tier,value])=><span key={tier}>{tier}：{String(value)}</span>)}{fallbackPlan&&<small>召回不足时：{fallbackPlan}</small>}</div>
        :<span className="strategy-group-targets">未提供预期召回</span>}
      </div>
    </div>}
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
