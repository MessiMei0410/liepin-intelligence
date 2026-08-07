import { AlertTriangle, ArrowUpRight, CalendarClock, CheckCircle2, ChevronLeft, ChevronRight, Clock3, LoaderCircle, MessagesSquare, Pin, Plus, Settings2, Sparkles } from 'lucide-react'
import { useState } from 'react'
import type { AnalysisTemplate, Dashboard, Workbench, WorkbenchItem, WorkbenchLane } from '../api'
import { workbenchLaneCount } from '../api'

type Lane = WorkbenchLane | 'templates'
const PAGE_SIZE = 10

const lanes: Array<[Lane, string]> = [
  ['decision', '待判断'], ['running', '运行中'], ['waiting_client', '待客户'], ['risk', '风险/逾期'], ['delivered', '最近交付'], ['templates', '固定分析'],
]

const laneEmptyText: Record<WorkbenchLane, string> = {
  decision: '当前没有待判断事项',
  running: '当前没有运行中任务',
  waiting_client: '当前没有待客户反馈的人选对',
  risk: '当前没有风险或逾期事项',
  delivered: '最近还没有交付物',
}

const laneCaption: Record<WorkbenchLane, string> = {
  decision: '需顾问判断',
  running: 'Agent 任务',
  waiting_client: '等客户反馈',
  risk: '超时/异常',
  delivered: '分析/报告',
}

const statusText: Record<string, string> = { running: '运行中', completed: '已完成', failed: '失败', skipped: '已跳过' }
const compactTime = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
}).format(new Date(value))
const scheduleText = (template: AnalysisTemplate) => {
  if (!template.schedule_enabled || template.schedule_kind === 'manual') return '手动运行'
  const prefix = template.schedule_kind === 'daily' ? '每天' : `每周${['一', '二', '三', '四', '五', '六', '日'][template.schedule_weekday]}`
  return `${prefix} ${template.schedule_time}${template.next_run_at ? ` · 下次 ${compactTime(template.next_run_at)}` : ''}`
}

function LaneIcon({ lane }: { lane: WorkbenchItem['lane'] }) {
  if (lane === 'delivered') return <CheckCircle2 />
  if (lane === 'running') return <LoaderCircle className="spin" />
  if (lane === 'risk') return <AlertTriangle />
  if (lane === 'waiting_client') return <MessagesSquare />
  return <Clock3 />
}

function WorkItemRow({ item, act }: { item: WorkbenchItem; act: (item: WorkbenchItem) => void }) {
  return <article className="workbench-row">
    <span className={`workbench-kind ${item.kind} lane-${item.lane}`} aria-hidden="true"><LaneIcon lane={item.lane} /></span>
    <div className="workbench-row-copy">
      <div><b>{item.title}</b><span className="workbench-source">{item.source_label}</span></div>
      <span>{item.subtitle}</span>
      {item.reason && <small>{item.reason}</small>}
    </div>
    <span className={`workbench-status ${item.kind === 'approval' || item.lane === 'risk' ? 'urgent' : ''}`}>{item.status_label}</span>
    <button className="icon-btn workbench-open" title={item.primary_action.label} aria-label={`${item.primary_action.label}：${item.title}`} onClick={() => act(item)}><ArrowUpRight /></button>
  </article>
}

export function TodayWorkbench({ dashboard, workbench, templates, onAction, onQuickAnalysis, onRunTemplate, onOpenTemplate, onCreateTemplate, onManageTemplate, busy }: {
  dashboard?: Dashboard; workbench: Workbench; templates: AnalysisTemplate[];
  onAction: (item: WorkbenchItem) => void; onQuickAnalysis: () => void;
  onRunTemplate: (templateId: string) => void; onOpenTemplate: (template: AnalysisTemplate) => void;
  onCreateTemplate: () => void; onManageTemplate: (template: AnalysisTemplate) => void; busy?: string;
}) {
  const [lane, setLane] = useState<Lane>('decision')
  const [page, setPage] = useState(0)
  const counts = dashboard?.counts || {}
  const items = lane === 'templates' ? [] : workbench.items.filter(item => item.lane === lane)
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE))
  const currentPage = Math.min(page, pageCount - 1)
  const visibleItems = items.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
  const laneTotal = lane === 'templates' ? templates.length : workbenchLaneCount(workbench, lane)
  const selectLane = (nextLane: Lane) => {
    setLane(nextLane)
    setPage(0)
  }
  return <div className="today-workbench">
    <section className="today-summary" aria-label="今日概况">
      {(Object.keys(laneCaption) as WorkbenchLane[]).map(id => <div key={id}>
        <span>{lanes.find(([laneId]) => laneId === id)?.[1]}</span><strong>{workbenchLaneCount(workbench, id)}</strong><small>{laneCaption[id]}</small>
      </div>)}
      <div><span>开放岗位</span><strong>{counts.active_jobs ?? '-'}</strong><small>当前在推</small></div>
    </section>

    <section className="workbench-band">
      <header className="workbench-band-head">
        <div><h2>今日工作台</h2><span>按风险与时效排序{workbench.truncated ? ` · 已加载 ${workbench.returned_count || workbench.items.length} / ${workbench.summary.total}` : ''}</span></div>
        {lane === 'templates'
          ? <button className="button primary" disabled={!!busy} onClick={onCreateTemplate}><Plus />新建固定分析</button>
          : <button className="button primary" disabled={!!busy} onClick={onQuickAnalysis}>{busy === 'quick' ? <LoaderCircle className="spin" /> : <Sparkles />}生成今日分析</button>}
      </header>
      <nav className="segmented workbench-segments" aria-label="工作台视图">
        {lanes.map(([id, label]) => <button key={id} className={lane === id ? 'active' : ''} onClick={() => selectLane(id)}>
          {label}<span>{id === 'templates' ? templates.length : workbenchLaneCount(workbench, id)}</span>
        </button>)}
      </nav>
      {lane !== 'templates' && <div className="workbench-list">
        {visibleItems.map(item => <WorkItemRow key={item.item_key} item={item} act={onAction} />)}
        {!items.length && <div className="workbench-empty"><CheckCircle2 /><b>{laneEmptyText[lane]}</b></div>}
        {items.length > PAGE_SIZE && <footer className="workbench-pagination" aria-label="工作台分页">
          <span>第 {currentPage + 1} / {pageCount} 页 · {workbench.truncated && laneTotal > items.length ? `已加载 ${items.length} / 共 ${laneTotal}` : `共 ${items.length}`} 项</span>
          <div>
            <button className="icon-btn" title="上一页" aria-label="上一页" disabled={currentPage === 0} onClick={() => setPage(value => Math.max(0, value - 1))}><ChevronLeft /></button>
            <button className="icon-btn" title="下一页" aria-label="下一页" disabled={currentPage >= pageCount - 1} onClick={() => setPage(value => Math.min(pageCount - 1, value + 1))}><ChevronRight /></button>
          </div>
        </footer>}
      </div>}
      {lane === 'templates' && <div className="template-list">
        {templates.map(template => <article key={template.template_id}>
          <Pin /><div><b>{template.name}</b><span>{template.last_result?.headline || template.question || '尚未运行'}</span><small><CalendarClock />{scheduleText(template)}{template.last_status ? ` · ${statusText[template.last_status] || template.last_status}` : ''}</small></div>
          <div className="template-actions">
            {template.last_run_id && <button className="icon-btn" title="查看最近结果" aria-label={`查看最近结果：${template.name}`} onClick={() => onOpenTemplate(template)}><ArrowUpRight /></button>}
            <button className="icon-btn" title="管理固定分析" aria-label={`管理固定分析：${template.name}`} onClick={() => onManageTemplate(template)}><Settings2 /></button>
            <button className="button" disabled={!!busy} onClick={() => onRunTemplate(template.template_id)}>{busy === template.template_id ? <LoaderCircle className="spin" /> : null}运行</button>
          </div>
        </article>)}
        {!templates.length && <div className="workbench-empty"><Pin /><b>还没有固定分析</b></div>}
      </div>}
    </section>
  </div>
}
