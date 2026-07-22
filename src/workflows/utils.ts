import { Workflow } from '../api'
import { recordValue, arrayValue } from '../shared/records'
import { zeroAttributionLabel } from '../workflow/statusMapping'

export const stepStatusLabel: Record<string,string> = {
  pending:'待执行', queued:'排队中', running:'执行中', waiting_approval:'等待审批', waiting_external:'等待外部结果',
  completed:'已完成', skipped:'已跳过', blocked:'已阻塞', failed:'失败', cancelled:'已取消',
}
// 渠道 ID → 中文名（猎聘/X-SaaS），utils 与 WorkflowFunnel 共用，不再各自内联。
export const channelLabel = (name: string) => name==='liepin'?'猎聘':name==='xsaas'?'X-SaaS':name||'渠道'
export const activeWorkflowStatuses = new Set(['queued','running','waiting_approval','waiting_external'])
export const stepTone = (status='') => ['completed','skipped'].includes(status) ? 'done' : ['running','queued','waiting_external'].includes(status) ? 'active' : status==='waiting_approval' ? 'needs-approval' : ['failed','blocked'].includes(status) ? 'error' : ['cancelled'].includes(status) ? 'muted' : 'pending'
export const humanizeWorkflowError = (error?: string) => {
  const text=String(error||'').trim()
  if(!text)return '执行失败，尚未记录明确原因。'
  if(text.includes('write_report')||text.includes('run_published_position_search.py')&&text.includes('FileNotFound'))return '寻访结果已获取，但保存报告失败：岗位名称包含路径分隔符。该问题已修复，可以重试本步骤。'
  if(text.includes('No such file or directory'))return '执行所需的文件或目录不存在。请修复路径后重试。'
  if(text.includes('Traceback'))return '执行脚本遇到异常，技术详情已隐藏。修复后可以重试本步骤。'
  return text.length>180?`${text.slice(0,180)}…`:text
}
export const humanizeWorkflowEvent = (event:NonNullable<Workflow['events']>[number]) => {
  const summary=String(event.summary||'')
  if(event.status==='failed'||summary.includes('Traceback'))return `渠道执行失败：${humanizeWorkflowError(summary)}`
  return summary
}
export const stepBusinessResult = (step:Workflow['steps'][number]) => {
  if(step.error)return {headline:humanizeWorkflowError(step.error),facts:['本步骤未完成，后续步骤尚未执行。'],error:true}
  let data=recordValue(step.output)
  if(!Object.keys(data).length&&step.output_json){try{data=recordValue(JSON.parse(step.output_json))}catch{/* Ignore legacy malformed output. */}}
  const facts:string[]=[]
  const diagnosis=recordValue(data.diagnosis)
  const funnel=recordValue(diagnosis.funnel)
  if(Object.keys(funnel).length)facts.push(`当前人才漏斗 ${Number(funnel.total||0)} 人，待复核 ${Number(funnel.pending_review||0)} 人。`)
  const candidates=arrayValue(data.candidates)
  if(candidates.length){const names=candidates.slice(0,3).map(item=>recordValue(item).display_name).filter(Boolean);facts.push(`读取 ${candidates.length} 条历史人岗关系${names.length?`：${names.join('、')}`:''}。`)}
  const strategy=recordValue(data.strategy)
  const channels=recordValue(strategy.channels)
  if(Object.keys(channels).length)facts.push(`寻访策略：猎聘 ${arrayValue(channels.liepin).length} 组关键词，X-SaaS ${arrayValue(channels.xsaas).length} 组关键词。`)
  const request=recordValue(data.auto_execute_request||data.external_request)
  const preflight=recordValue(recordValue(request.preflight).preflight)
  const readyChannels=recordValue(preflight.channels)
  if(Object.keys(readyChannels).length){const ready=Object.entries(readyChannels).filter(([,item])=>recordValue(item).ready).map(([name])=>name==='liepin'?'猎聘':name==='xsaas'?'X-SaaS':name);facts.push(`渠道已就绪：${ready.join('、')||'等待浏览器连接'}。`)}
  const external=recordValue(data.external_result)
  const runs=arrayValue(external.channel_runs)
  if(runs.length){
    // R8：0 候选的渠道不得只显示 completed——有质量标记/归因时展示“0 条候选 · 归因”，否则回落原状态文案。
    const labels=runs.map(item=>{
      const run=recordValue(item)
      const channel=channelLabel(String(run.channel||''))
      const produced=Number(recordValue(run.result).candidates||0)
      const quality=String(run.quality||'')
      const attribution=zeroAttributionLabel(String(run.zero_attribution||''))
      if(produced>0)return `${channel} ${produced} 条候选`
      if(quality.startsWith('zero')||attribution)return `${channel} 0 条候选 · ${attribution||'质量未知，原因待排查'}`
      return `${channel} ${String(run.status||'已返回')}`
    })
    facts.push(`渠道结果：${labels.join('，')}。`)
  }
  const shadow=recordValue(external.opencli_shadow)
  const shadowChannels=arrayValue(shadow.channels)
  if(shadow.enabled&&shadowChannels.length){
    const labels=shadowChannels.map(item=>{const row=recordValue(item);const comparison=recordValue(row.comparison);const channel=row.channel==='liepin'?'猎聘':row.channel==='xsaas'?'X-SaaS':String(row.channel||'渠道');return row.status==='completed'?`${channel} 重合 ${Number(comparison.overlap||0)}/${Number(comparison.baseline_count||0)}`:`${channel} ${row.status==='blocked'?'受阻':'跳过'}`})
    facts.push(`OpenCLI 只读影子：${labels.join('，')}；未参与入库。`)
  }
  const appliedIntake=recordValue(recordValue(recordValue(external.intake).applied).intake)
  if(Object.keys(appliedIntake).length)facts.push(`本轮新增入库 ${Number(appliedIntake.inserted||0)} 人，跳过已有 ${Number(appliedIntake.skipped_existing||0)} 人。`)
  const artifacts=arrayValue(data.artifacts)
  if(artifacts.length)facts.push(`已生成 ${artifacts.length} 个业务产物。`)
  const verification=recordValue(step.verification)
  if(verification.status==='verified')facts.push('执行后校验已通过。')
  const recovery=recordValue(step.recovery)
  if(recovery.action==='retry_same_step')facts.push(`已按恢复计划自动重试 ${Number(recovery.attempt||1)} 次。`)
  const fallback=step.status==='completed'?'步骤已完成。':step.status==='waiting_external'?'正在等待渠道返回寻访结果。':step.status==='waiting_approval'?'批准后才会执行外部寻访。':'尚未执行。'
  return {headline:String(data.summary||fallback),facts:[...new Set(facts)],error:false}
}
