import { useState } from 'react'
import { Archive, MessageSquareText, Radar } from 'lucide-react'
import { Dashboard, DashboardCounts, DashboardWorkflow, Job, Candidate } from '../api'
import { Metric, SectionHead } from '../shared/primitives'
import { Candidates } from './Candidates'
import { stageTone } from '../shared/format'
import { activeWorkflowStatuses } from '../workflows/utils'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import { RadarPage } from './Radar'

// dashboard workflows[] 与 /summary 同源透传 business_outcome，标签统一走 statusMapping（AGENTS.md 硬性约定）
const workflowTag = (w: DashboardWorkflow) => mapWorkflowStatus({ status: w.status, business_outcome: w.business_outcome }).label

export function Overview({ dashboard, jobs, candidates, openWorkflow, openCandidate, archiveWorkflow, openCopilot }: { dashboard?: Dashboard; jobs: Job[]; candidates: Candidate[]; openWorkflow: (id: string) => void | Promise<void>; openCandidate: (id: number) => void | Promise<void>; archiveWorkflow: (id: string) => void | Promise<void>; openCopilot?: () => void }) {
  const [radarOpen, setRadarOpen] = useState(false)
  if (!dashboard) {
    return (
      <>
        <section className="metrics">
          <div className="skeleton-card"><span className="skeleton-block" style={{width:'60%',height:18}} /><span className="skeleton-block" style={{width:'40%',height:14}} /></div>
          <div className="skeleton-card"><span className="skeleton-block" style={{width:'50%',height:18}} /><span className="skeleton-block" style={{width:'35%',height:14}} /></div>
          <div className="skeleton-card"><span className="skeleton-block" style={{width:'55%',height:18}} /><span className="skeleton-block" style={{width:'45%',height:14}} /></div>
          <div className="skeleton-card"><span className="skeleton-block" style={{width:'45%',height:18}} /><span className="skeleton-block" style={{width:'30%',height:14}} /></div>
        </section>
        <div className="overview-grid">
          <section className="section">
            <span className="skeleton-block" style={{width:'120px',height:16,marginBottom:12}} />
            <div className="skeleton-block" style={{height:52,marginBottom:8}} />
            <div className="skeleton-block" style={{height:52,marginBottom:8}} />
            <div className="skeleton-block" style={{height:52}} />
          </section>
          <section className="section">
            <span className="skeleton-block" style={{width:'100px',height:16,marginBottom:12}} />
            <div className="skeleton-block" style={{height:48,marginBottom:8}} />
            <div className="skeleton-block" style={{height:48,marginBottom:8}} />
            <div className="skeleton-block" style={{height:48}} />
          </section>
        </div>
      </>
    )
  }
  const counts: DashboardCounts = dashboard?.counts || {}
  return <>
    <section className="metrics">
      <Metric label="在推岗位" value={counts.active_jobs ?? '-'} detail="规范岗位实体" />
      <Metric label="候选关系" value={counts.candidates ?? '-'} detail="人选 × 岗位" />
      <Metric label="待处理" value={counts.pending_candidates ?? '-'} detail="已排除停止推进" />
      <Metric label="待审批" value={counts.pending_approvals ?? '-'} detail="工作流单次确认" />
    </section>
    <div className="overview-grid">
      {((counts.pending_proposals ?? 0) + (counts.failed_proposals ?? 0)) > 0 && <section className="section agent-proposal-entry"><SectionHead title="Agent 行动卡" meta={`待确认 ${counts.pending_proposals ?? 0} · 已执行 ${counts.executed_proposals ?? 0} · 失败 ${counts.failed_proposals ?? 0}`} /><button className="button secondary" onClick={openCopilot}><MessageSquareText/><span>在 Copilot 查看</span></button></section>}
      <section className="section"><SectionHead title="当前工作流" meta={`${dashboard?.workflows?.length || 0} 条`} />
        <div className="rows">{dashboard?.workflows?.map((w) => <div className="work-row-shell" key={w.workflow_id}><button className="work-row" onClick={() => openWorkflow(w.workflow_id)}><span className={`dot ${stageTone(w.status)}`}/><div><b>{w.title}</b><small>{w.workflow_id} · {w.current_stage || '未开始'}</small></div><span className="tag">{workflowTag(w)}</span></button>{!activeWorkflowStatuses.has(w.status)&&<button className="row-icon-action" title="归档工作流" aria-label={`归档工作流 ${w.title}`} onClick={()=>archiveWorkflow(w.workflow_id)}><Archive/></button>}</div>)}</div>
      </section>
      <section className="section"><SectionHead title="优先岗位" meta="按活跃人选排序" />
        <div className="rows">{jobs.slice(0, 6).map((j: Job) => <div className="compact-row" key={j.id}><div><b>{j.title}</b><small>{j.client} · {j.priority || j.status}</small></div><strong>{j.active_candidate_count || 0}</strong></div>)}</div>
      </section>
    </div>
    <section className="section"><SectionHead title="最近更新人选" meta="实时数据库" /><Candidates items={candidates.slice(0, 8)} openCandidate={openCandidate} compact /></section>
    <section className="overview-secondary">
      <button className="button secondary" aria-expanded={radarOpen} onClick={() => setRadarOpen(value => !value)}><Radar/><span>人才雷达</span></button>
      {radarOpen && <div className="overview-radar"><RadarPage jobs={jobs} /></div>}
    </section>
  </>
}
