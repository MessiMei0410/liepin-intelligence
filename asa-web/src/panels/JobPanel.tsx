import { useState } from 'react'
import { ChevronLeft, MessageSquareText, X, ShieldAlert, Route, UserRoundSearch, ChevronRight, CircleCheck, CircleDashed, Check, ExternalLink } from 'lucide-react'
import { JobDetail } from '../api'
import { JobProfileInsights } from './JobProfileInsights'
import { JobBrief } from './JobBrief'
import { DialogPanel } from '../shared/Dialog'
import type { DragResizeAnchor } from '../shared/dialogDragResize'
import { nativeBridge } from '../shared/nativeBridge'
import { RecommendationMetricsCard } from './RecommendationMetricsCard'
import { JobWeeklyReport } from './JobWeeklyReport'
import { SourcingAdjustments } from './SourcingAdjustments'
import { recordValue, textList } from '../shared/records'
import { date, sourceLabel, eventStatusLabel, lifecycleEventLabel, lifecycleEventTone, stageTone } from '../shared/format'
import { SectionHead } from '../shared/primitives'
import { openAgentWorkspace } from '../agent/navigation'

// 岗位详情长内容可读性打磨：
// - 列表类内容统一「先显示前 N 条 + 截断说明 + 展开全部/收起」，数量一目了然；
// - 岗位人选用限高滚动容器 + content-visibility 兜底长列表，打开人选动作保持全量可用；
// - 寻访策略按「顾问约束/关键词/目标公司/可接受职级」分区，每块带数量徽标。
const FOLLOWUP_LIMIT = 8
const EVENT_LIMIT = 10
const EXPERIMENT_LIMIT = 8

function LimitNote({ total, limit, expanded, onToggle, unit = '条' }: {
  total: number
  limit: number
  expanded: boolean
  onToggle: () => void
  unit?: string
}) {
  if (total <= limit) return null
  return (
    <div className="job-limit-note">
      <span>{expanded ? `已展开全部 ${total} ${unit}。` : `仅显示前 ${limit} ${unit}，共 ${total} ${unit}。`}</span>
      <button type="button" className="button" onClick={onToggle}>{expanded ? '收起' : `展开全部 ${total} ${unit}`}</button>
    </div>
  )
}

export function JobPanel({ value, close, openCandidate, changed }: { value: JobDetail; close: () => void; openCandidate: (id: number) => void; changed?: () => void | Promise<void> }) {
  const [followupExpanded, setFollowupExpanded] = useState(false)
  const [eventExpanded, setEventExpanded] = useState(false)
  const [experimentExpanded, setExperimentExpanded] = useState(false)
  const position = recordValue(value.position)
  const profile = recordValue(value.profile)
  const hard = textList(profile.hard_requirements, position.hard_requirements, value.hard_requirements)
  const abilities = textList(profile.ability_keywords, position.ability_keywords, value.ability_keywords)
  const targets = textList(profile.target_companies, position.target_companies, value.target_companies, value.metric_target_companies)
  const exclusions = textList(profile.exclusion_tags, position.exclusions, value.exclusions, value.exclude_terms)
  const pitch = textList(profile.pitch_points)
  const risks = textList(profile.risk_points, value.risk)
  const strategy = value.latest_effective_strategy
  const strategyKeywords = strategy?.keyword_groups.flatMap(group => group.terms) || []
  const strategyCompanies = strategy?.company_tiers.flatMap(group => group.companies) || []
  const strategyLevels = textList(strategy?.level_mapping.accepted_levels)
  const strategyFallback = String(strategy?.expectation.fallback_plan || '')
  const maxStage = Math.max(1, ...value.stages.map(item => item.count))
  // 弹出为独立窗口：macOS 宿主 openDetachedDialog 打开可自由拖出屏幕的原生窗口。
  const detachPanel = (anchor?: DragResizeAnchor): boolean => {
    if (nativeBridge('openDetachedDialog', { title: value.title, url: `/asa-app#job=${value.id}&bare=1`, anchor })) {
      close()
      return true
    }
    return false
  }
  const visibleFollowups = value.followups.slice(0, followupExpanded ? value.followups.length : FOLLOWUP_LIMIT)
  const visibleEvents = value.events.slice(0, eventExpanded ? value.events.length : EVENT_LIMIT)
  const visibleExperiments = value.search_experiments.slice(0, experimentExpanded ? value.search_experiments.length : EXPERIMENT_LIMIT)
  const facts: Array<[string, unknown]> = [
    ['优先级', value.priority || '常规'], ['状态', value.status || value.lifecycle_stage || '待启动'],
    ['地点', position.location || value.location || '待确认'], ['薪资', position.salary || '待确认'],
    ['编制', position.headcount ?? '待确认'], ['截止', position.deadline || '待确认'],
    ['学历', position.education || profile.education_requirement || '待确认'], ['经验', position.experience || profile.experience_requirement || '待确认'],
  ]
  return (
    <DialogPanel panelClassName="detail-panel job-detail-panel" onEscape={close} onDetach={detachPanel}>
        <header className="detail-head" style={{ cursor: 'grab', userSelect: 'none', touchAction: 'none' }} title="按住拖动；拖出屏幕边缘可弹出为独立窗口">
          <button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ChevronLeft /></button>
          <div><h2>{value.title}</h2><p>{value.client} · {String(position.department || position.team || value.location || '岗位详情')} · 岗位 #{value.id}</p></div>
          <div className="detail-actions">
            <button className="button" onClick={() => openAgentWorkspace({ type: 'job', id: value.id, client: value.client, job: value.title })}><MessageSquareText />交给 Agent</button>
            {value.priority?.includes('P0') && <span className="tag warn">P0 最急</span>}
            <button className="icon-btn candidate-dialog-detach" onClick={() => void detachPanel()} title="弹出为独立窗口（可拖出屏幕）" aria-label="弹出为独立窗口"><ExternalLink /></button>
            <button className="icon-btn" onClick={close} title="关闭" aria-label="关闭"><X /></button>
          </div>
        </header>
        <div className="job-detail-body">
          <main>
            <section className="job-funnel" aria-label="岗位漏斗">
              <div><span>全部人选</span><b>{value.funnel.total}</b></div>
              <div><span>活跃推进</span><b>{value.funnel.active}</b></div>
              <div><span>已触达</span><b>{value.funnel.contacted}</b></div>
              <div><span>已推荐</span><b>{value.funnel.recommended}</b></div>
              <div><span>已停止</span><b>{value.funnel.stopped}</b></div>
            </section>
            <RecommendationMetricsCard jobId={value.id} />
            <JobWeeklyReport jobId={value.id} />
            <SourcingAdjustments jobId={value.id} />
            <section className="job-detail-section">
              <h3>岗位概况</h3>
              <div className="job-facts">{facts.map(([label, content]) => <div key={label}><span>{label}</span><b>{String(content)}</b></div>)}</div>
              {(profile.jd_analysis_summary || value.summary) && <p className="job-summary">{String(profile.jd_analysis_summary || value.summary)}</p>}
              {value.closed_reason && <p className="job-warning">{value.closed_reason}</p>}
            </section>
            <JobBrief job={value} />
            <JobProfileInsights key={value.id} jobId={value.id} onChanged={changed} />
            <JobListSection title="硬性要求" items={hard} />
            <JobListSection title="核心能力" items={abilities} />
            <JobListSection title="岗位卖点" items={pitch} />
            <div className="job-detail-columns"><JobListSection title="目标公司" items={targets} /><JobListSection title="排除条件" items={exclusions} tone="warn" /></div>
            <section className="job-detail-section">
              <h3>阶段分布<span className="job-section-count">{value.stages.length} 个阶段</span></h3>
              {value.stages.length ? <div className="job-stage-list">{value.stages.map(item => <div key={item.stage}><span>{item.stage}</span><i><b style={{ width: `${Math.max(5, item.count / maxStage * 100)}%` }} /></i><strong>{item.count}</strong></div>)}</div> : <div className="empty">当前岗位还没有候选人阶段记录。</div>}
            </section>
            <section className="job-detail-section" aria-label="当前寻访策略">
              <h3>当前寻访策略{strategy && <span className="job-section-count">v{strategy.plan_version}</span>}</h3>
              {strategy ? (
                <>
                  <p className="job-summary">{strategy.summary || '策略已生成，等待执行验证。'} · 计划 v{strategy.plan_version}{strategy.generated_at ? ` · 生成于 ${date(String(strategy.generated_at))}` : ''}</p>
                  {strategy.consultant_constraints.length > 0 && (
                    <div className="job-strategy-block">
                      <div className="job-strategy-label"><b>顾问约束</b><em>{strategy.consultant_constraints.length} 条</em></div>
                      <div className="job-list-items">{strategy.consultant_constraints.map(item => <div key={`${item.type}:${item.rule}`}><Check /><span title={item.rule}>{item.rule}</span></div>)}</div>
                    </div>
                  )}
                  {strategyKeywords.length > 0 && (
                    <div className="job-strategy-block">
                      <div className="job-strategy-label"><b>关键词</b><em>{strategyKeywords.length} 个</em></div>
                      <div className="job-keywords">{strategyKeywords.map(item => <span key={item} title={item}>{item}</span>)}</div>
                    </div>
                  )}
                  {strategyCompanies.length > 0 && (
                    <div className="job-strategy-block">
                      <div className="job-strategy-label"><b>目标公司</b><em>{strategyCompanies.length} 家</em></div>
                      <div className="job-keywords muted">{strategyCompanies.map(item => <span key={item} title={item}>{item}</span>)}</div>
                    </div>
                  )}
                  {strategyLevels.length > 0 && (
                    <div className="job-strategy-block">
                      <div className="job-strategy-label"><b>可接受职级</b><em>{strategyLevels.length} 个</em></div>
                      <div className="job-keywords muted">{strategyLevels.map(item => <span key={item} title={item}>{item}</span>)}</div>
                    </div>
                  )}
                  {strategyFallback && <p className="job-summary">扩展路径：{strategyFallback}</p>}
                </>
              ) : <div className="empty">当前岗位还没有生效的寻访策略。</div>}
              {risks.length > 0 && <div className="job-risk-list">{risks.map(item => <p key={item}><ShieldAlert />{item}</p>)}</div>}
            </section>
            <section className="job-detail-section" aria-label="寻访记录">
              <h3>寻访记录<span className="job-section-count">{value.search_experiments.length} 条</span></h3>
              {value.search_experiments.length > 0 ? (
                <div className="job-experiments">
                  {visibleExperiments.map((item, index) => (
                    <div key={item.id ?? index} title={String(item.query || '')}>
                      <Route />
                      <div><b>{String(item.query || '未记录关键词')}</b><span>{sourceLabel(String(item.channel || ''))} · 结果 {Number(item.result_count || 0)} · 提取 {Number(item.extracted_count || 0)} · 推荐 {Number(item.recommended_count || 0)}</span></div>
                      <small>{date(String(item.updated_at || item.run_time || ''))}</small>
                    </div>
                  ))}
                  <LimitNote total={value.search_experiments.length} limit={EXPERIMENT_LIMIT} expanded={experimentExpanded} onToggle={() => setExperimentExpanded(expanded => !expanded)} />
                </div>
              ) : <div className="empty">暂无历史寻访记录。</div>}
            </section>
          </main>
          <aside>
            <SectionHead title="岗位人选" meta={`${value.candidates.length} 人`} />
            {value.candidates.length === 0
              ? <div className="aside-empty"><UserRoundSearch /><span>当前岗位还没有人选，可交给 Agent 启动寻访</span></div>
              : <div className="job-candidate-list" aria-label="岗位人选列表">
                  {value.candidates.map(candidate => (
                    <button key={candidate.id} type="button" onClick={() => openCandidate(candidate.id)} aria-label={`打开候选人 ${candidate.name}`} title={`${candidate.name} · ${candidate.current_company || '公司待补充'} · ${candidate.current_title || '职位待补充'}`}>
                      <UserRoundSearch />
                      <div><b>{candidate.name}</b><span>{candidate.current_company || '公司待补充'} · {candidate.current_title || '职位待补充'}</span><small className={`tag ${stageTone(candidate.clean_stage || candidate.flow_bucket)}`}>{candidate.clean_stage || candidate.flow_bucket || '待复核'}</small></div>
                      <ChevronRight />
                    </button>
                  ))}
                </div>}
            <SectionHead title="待办" meta={`${value.followups.length} 条`} />
            {value.followups.length === 0
              ? <div className="aside-empty"><CircleCheck /><span>当前没有岗位待办</span></div>
              : <div className="job-followups">
                  {visibleFollowups.map((item, index) => (
                    <div className="aside-item" key={item.id ?? index}>
                      <b>{String(item.candidate_name || item.task_type || '岗位待办')}</b>
                      <span>{String(item.reason || '待处理')}</span>
                      <small>{item.due_at ? `截止 ${date(String(item.due_at))}` : '未设置截止时间'}</small>
                    </div>
                  ))}
                  <LimitNote total={value.followups.length} limit={FOLLOWUP_LIMIT} expanded={followupExpanded} onToggle={() => setFollowupExpanded(expanded => !expanded)} />
                </div>}
            <SectionHead title="最近动态" meta={`${value.events.length} 条`} />
            {value.events.length === 0
              ? <div className="aside-empty"><CircleDashed /><span>当前没有业务动态</span></div>
              : <div className="timeline">
                  {visibleEvents.map(event => (
                    <div key={event.id} title={event.summary || event.event_type}>
                      <i className={lifecycleEventTone(event.event_type)||undefined} /><span>{date(event.event_time)}</span><b>{event.summary || lifecycleEventLabel(event.event_type) || event.event_type}</b><small>{eventStatusLabel(event.event_status)}</small>
                    </div>
                  ))}
                  <LimitNote total={value.events.length} limit={EVENT_LIMIT} expanded={eventExpanded} onToggle={() => setEventExpanded(expanded => !expanded)} />
                </div>}
          </aside>
        </div>
    </DialogPanel>
  )
}

export function JobListSection({ title, items, tone = '' }: { title: string; items: string[]; tone?: string }) {
  if (!items.length) return null
  return (
    <section className={`job-detail-section ${tone}`}>
      <h3>{title}<span className="job-section-count">{items.length} 项</span></h3>
      <div className="job-list-items">{items.map(item => <div key={item}><Check /><span title={item}>{item}</span></div>)}</div>
    </section>
  )
}
