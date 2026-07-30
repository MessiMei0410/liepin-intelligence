import { useEffect, useState } from 'react'
import { ListFilter, LoaderCircle, ShieldCheck } from 'lucide-react'
import { api } from '../api'
import type { CoverageCertificate, SourcingFunnel, SourcingFunnelChannel, SourcingFunnelRun } from '../workflow/sourcingFunnel'
import { channelQueryCount, channelRuns, queryText } from '../workflow/sourcingFunnel'
import { zeroAttributionLabel, zeroAttributionTone } from '../workflow/statusMapping'
import { channelLabel } from './utils'

// R8 渠道寻访漏斗：独立按需路由 /sourcing-funnel，面板挂载与详情刷新（updatedAt 变化）时拉取，
// 不进 /summary 轮询签名——R7 的轮询负载不回升。历史轮次（漏斗表无行）显示"该轮未记录渠道明细"。
export function WorkflowFunnel({workflowId,updatedAt}:{workflowId:string;updatedAt:string}) {
  const [data,setData]=useState<SourcingFunnel|null>(null)
  const [failed,setFailed]=useState(false)
  useEffect(()=>{
    let alive=true
    api.workflowSourcingFunnel(workflowId)
      .then(funnel=>{if(alive){setData(funnel);setFailed(false)}})
      .catch(()=>{if(alive)setFailed(true)})
    return()=>{alive=false}
  },[workflowId,updatedAt])

  const channels=data?.channels||[]
  const headSummary=data===null
    ?'明细加载中…'
    :channels.length
      ?channels.map(channel=>`${channelLabel(channel.channel)} 召回 ${channel.recall_count}`).join(' · ')
      :'该轮未记录渠道明细'
  return <section className="workflow-insight workflow-funnel" aria-label="渠道寻访漏斗">
    <header><span className="insight-icon"><ListFilter/></span><div><span>渠道漏斗</span><b>{headSummary}</b></div>{failed&&<span className="tag warn">明细加载失败，稍后自动重试</span>}</header>
    {data?.coverage_certificate&&<CoverageCertificateBlock certificate={data.coverage_certificate}/>}
    {data!==null&&channels.map(channel=><ChannelBlock key={channel.channel} funnel={data} channel={channel}/>)}
    {data!==null&&channels.length===0&&<div className="insight-empty">该轮未记录渠道明细（漏斗指标在此轮寻访之后上线，或寻访尚未执行）。</div>}
    {data===null&&!failed&&<div className="insight-empty"><LoaderCircle className="spin"/>渠道明细加载中…</div>}
  </section>
}

function CoverageCertificateBlock({certificate}:{certificate:CoverageCertificate}) {
  const cells=certificate.query_cells
  const recall=certificate.candidate_recall
  const integrity=certificate.evidence_integrity
  const dimensions=certificate.dimension_execution?.dimensions
  const status=['approved_query_cells_exhausted','approved_grid_exhausted'].includes(certificate.coverage_status)
    ?'查询单元已穷尽'
    :certificate.coverage_status==='platform_truncated'
      ?'平台截断'
      :'覆盖未知'
  const tone=['approved_query_cells_exhausted','approved_grid_exhausted'].includes(certificate.coverage_status)?'done':'warn'
  return <div className={`coverage-certificate ${tone}`}>
    <div className="coverage-certificate-head">
      <ShieldCheck/>
      <b>覆盖证明</b>
      <span className={`tag ${tone}`}>{status}</span>
    </div>
    <div className="coverage-certificate-metrics">
      <span>查询单元 <b>{cells.executed} / {cells.approved}</b></span>
      <span>穷尽 <b>{cells.exhausted}</b></span>
      <span>截断 <b>{cells.platform_capped}</b></span>
      <span>原始召回 <b>{recall.raw_occurrences}</b></span>
      <span>唯一身份 <b>{recall.unique_identities}</b></span>
      <span>低分留痕 <b>{recall.below_threshold}</b></span>
      {integrity&&<span>证据链 <b>{integrity.passed?'完整':'异常'}</b></span>}
      {dimensions&&<span>平台筛选 <b>{certificate.dimension_execution?.platform_filters_applied.length?'部分执行':'未使用'}</b></span>}
    </div>
    <p><b>{certificate.claims.defensible_claim}</b><span>平台候选人总体未知</span></p>
  </div>
}

function ChannelBlock({funnel,channel}:{funnel:SourcingFunnel;channel:SourcingFunnelChannel}) {
  const runs=channelRuns(funnel,channel.channel)
  const queryCount=channelQueryCount(funnel,channel.channel)
  const attribution=zeroAttributionLabel(channel.zero_attribution)
  const tone=zeroAttributionTone(channel.zero_attribution)
  // unknown 归因时附最新一条 error 摘要辅助排查（ROUND2 口径），其余归因自身已说明原因。
  const errorDigest=channel.zero_attribution==='unknown'?runs.map(run=>String(run.error||'').trim()).find(Boolean):undefined
  return <div className={`funnel-channel ${attribution?tone:''}`}>
    <div className="funnel-channel-head">
      <b>{channelLabel(channel.channel)}</b>
      {attribution&&<span className={`tag ${tone}`}>{attribution}</span>}
    </div>
    <div className="funnel-line">
      <span>查询 <b>{queryCount}</b> 组</span><i>→</i>
      <span>召回 <b>{channel.recall_count}</b></span><i>→</i>
      <span>抽取 <b>{channel.extracted_count}</b></span><i>→</i>
      <span>排重后 <b>{channel.unique_count}</b></span><i>→</i>
      <span>详情（完整 <b>{channel.detail.complete}</b> / 部分 <b>{channel.detail.partial}</b> / 失败 <b>{channel.detail.failed}</b>）</span><i>→</i>
      <span>入库新增 <b>{channel.intake_new_count}</b>（排重命中 {channel.intake_duplicate_count}）</span><i>→</i>
      <span>评估 <b>{channel.assessed_count}</b>（高分 {channel.high_score_count}）</span>
    </div>
    {errorDigest&&<p className="funnel-error">最近错误：{errorDigest.slice(0,160)}</p>}
    {runs.map(run=><QueryList key={String(run.run_id||run.created_at||'run')} run={run}/>)}
  </div>
}

function QueryList({run}:{run:SourcingFunnelRun}) {
  const entries=run.queries||[]
  if(!entries.length)return null
  return <details className="funnel-queries">
    <summary>查询明细（{Number(run.query_count)||entries.length} 组）</summary>
    <ul>{entries.map((entry,index)=>{
      const text=queryText(entry)
      const recall=Number(entry.result_count||0)
      const extracted=Number(entry.extracted_count||0)
      const abnormal=String(entry.reason||'')||(['stale_query'].includes(String(entry.status||''))?String(entry.status):'')
      return <li key={index}>
        <span className="funnel-query-text">{text||'（空查询）'}</span>
        <span className="funnel-query-stat">召回 {recall}{extracted?` · 抽取 ${extracted}`:''}</span>
        {abnormal&&<span className="tag warn">{abnormal}</span>}
      </li>
    })}</ul>
  </details>
}
