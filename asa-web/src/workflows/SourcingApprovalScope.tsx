type QueryPlan = {
  cell_count?: number
  dimensions?: Record<string, string[]>
  execution_semantics?: {
    retrieval_axes?: string[]
    platform_filters?: string[]
  }
}

const axisLabel: Record<string, string> = { channel: '渠道', query: '关键词' }
const dimensionLabel: Record<string, string> = { locations: '地点', levels: '职级', scenarios: '场景' }

export function SourcingApprovalScope({queryPlan}:{queryPlan?:QueryPlan}) {
  if(!queryPlan?.execution_semantics)return null
  const axes=(queryPlan.execution_semantics.retrieval_axes||[]).map(value=>axisLabel[value]||value)
  const constraints=Object.entries(queryPlan.dimensions||{})
    .filter(([,values])=>Array.isArray(values)&&values.length>0)
    .map(([key,values])=>`${dimensionLabel[key]||key}：${values.join('、')}`)
  return <div className="approval-sourcing-scope" aria-label="寻访执行范围">
    <p><span>检索轴</span>{axes.join(' + ')||'渠道 + 关键词'}（{Number(queryPlan.cell_count)||0} 个查询单元）</p>
    <p><span>评估约束</span>{constraints.join('；')||'无'}</p>
    <small>地点、职级、场景用于召回后评估，不作为平台筛选。</small>
  </div>
}
