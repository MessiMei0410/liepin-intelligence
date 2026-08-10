import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, Database, MessageSquareText, Pin, ShieldAlert, Wifi, X } from 'lucide-react'
import { api, AnalysisCatalogItem, AnalysisResult, AnalysisTemplate, AnalysisTemplateInput, AnalysisTrend, Bootstrap, Candidate, CandidateDetail, Dashboard, Job, JobDetail, Workbench, WorkbenchItem, Workflow } from '../api'
import { Tab, tabs } from '../shared/tabs'
import { Jobs } from '../pages/Jobs'
import { Candidates } from '../pages/Candidates'
import { Progress } from '../pages/Progress'
import { JobPanel } from '../panels/JobPanel'
import { CandidatePanel } from '../panels/CandidatePanel'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { SourcingCandidatesPage } from '../pages/SourcingCandidatesPage'
import { resolveWorkflowRevision } from '../workflow/workflowRevision'
import { Diagnostics } from './Diagnostics'
import { AnalysisWorkspace } from '../pages/AnalysisWorkspace'
import { AnalysisTemplateDialog } from '../components/AnalysisTemplateDialog'
import { AgentWorkspace } from '../agent/AgentWorkspace'
import { AGENT_NAVIGATE_EVENT } from '../agent/navigation'
import type { AgentContext, AgentReference } from '../agent/transport'
import { useGlobalDialogDrag } from '../shared/useGlobalDialogDrag'

const emptyWorkbench: Workbench = { ok: true, version: 'loading', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [] }

const analysisScopeLabels: Record<string, string> = {
  days: '统计周期', job_id: '岗位', candidate_id: '候选人', client: '客户', job: '岗位名称',
  candidate: '候选人名称', channel: '渠道', start_date: '开始日期', end_date: '结束日期',
}

const analysisScopeValue = (key: string, value: unknown) => {
  if (key === 'days') return `近 ${String(value)} 天`
  if (key === 'job_id' || key === 'candidate_id') return `#${String(value)}`
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

const analysisTime = (value: string) => {
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(timestamp)
}

export function App() {
  const [tab, setTab] = useState<Tab>('agent')
  const [boot, setBoot] = useState<Bootstrap>()
  const [dashboard, setDashboard] = useState<Dashboard>()
  const [jobs, setJobs] = useState<Job[]>([])
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [job, setJob] = useState<JobDetail>()
  const [candidate, setCandidate] = useState<CandidateDetail>()
  const [workflow, setWorkflow] = useState<Workflow>()
  const [sourcingCandidatesWorkflowId, setSourcingCandidatesWorkflowId] = useState('')
  const [workbench, setWorkbench] = useState<Workbench>()
  const [templates, setTemplates] = useState<AnalysisTemplate[]>([])
  const [analysisCatalog, setAnalysisCatalog] = useState<AnalysisCatalogItem[]>([])
  const [analysis, setAnalysis] = useState<AnalysisResult>()
  const [analysisTrend, setAnalysisTrend] = useState<AnalysisTrend>()
  const [analysisTemplateId, setAnalysisTemplateId] = useState('')
  const [templateDialog, setTemplateDialog] = useState<AnalysisTemplate | 'new'>()
  const [analysisBusy, setAnalysisBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [coreOffline, setCoreOffline] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [agentContext, setAgentContext] = useState<AgentContext>({ type: 'page', page: 'agent', mode: 'page_review' })
  const candidateStateRef = useRef<CandidateDetail | undefined>(undefined)
  const coreFailuresRef = useRef(0)
  const workbenchRefreshRef = useRef(0)
  useGlobalDialogDrag()

  // Core 健康探测：连续 2 次失败才判定离线；成功一次即复位，不重复打搅。
  const probeCore = async () => {
    try {
      await api.health()
      coreFailuresRef.current = 0
      setCoreOffline(false)
    } catch {
      coreFailuresRef.current += 1
      if (coreFailuresRef.current >= 2) setCoreOffline(true)
    }
  }

  const refreshWorkbench = async () => {
    const refreshId = ++workbenchRefreshRef.current
    await Promise.allSettled([
      api.workbench().then(next => {
        if (refreshId === workbenchRefreshRef.current && Array.isArray(next.items) && next.summary) setWorkbench(next)
      }),
      api.analyticsTemplates().then(next => {
        if (refreshId === workbenchRefreshRef.current && Array.isArray(next.items)) setTemplates(next.items)
      }),
    ])
  }

  useEffect(() => {
    let active = true
    api.bootstrap()
      .then(value => { if (active) setBoot(value) })
      .catch(e => { if (active) setError(String(e.message || e)) })
    Promise.allSettled([api.dashboard(), api.allJobs(), api.allCandidates()])
      .then(results => {
        if (!active) return
        const [dashboardResult, jobsResult, candidatesResult] = results
        const failures: string[] = []
        if (dashboardResult.status === 'fulfilled') setDashboard(dashboardResult.value)
        else failures.push(`经营概况：${String(dashboardResult.reason?.message || dashboardResult.reason)}`)
        if (jobsResult.status === 'fulfilled') setJobs(jobsResult.value.items)
        else failures.push(`岗位看板：${String(jobsResult.reason?.message || jobsResult.reason)}`)
        if (candidatesResult.status === 'fulfilled') setCandidates(candidatesResult.value.items)
        else failures.push(`人选模块：${String(candidatesResult.reason?.message || candidatesResult.reason)}`)
        if (failures.length) setError(`部分模块加载失败。${failures.join('；')}`)
        else setError('')
      })
    queueMicrotask(() => void refreshWorkbench())
    queueMicrotask(() => {
      void api.analyticsCatalog().then(result => setAnalysisCatalog(result.items)).catch(() => undefined)
    })
    return () => { active = false }
  }, [refreshKey])

  useEffect(() => {
    let active = true
    let refreshing = false
    const refresh = async () => {
      if (!active || refreshing || document.hidden) return
      refreshing = true
      try { await Promise.all([refreshWorkbench(), probeCore()]) } finally { refreshing = false }
    }
    queueMicrotask(() => void probeCore())
    const timer = window.setInterval(() => void refresh(), 15_000)
    const onVisible = () => { if (!document.hidden) void refresh() }
    window.addEventListener('focus', onVisible)
    document.addEventListener('visibilitychange', onVisible)
    const source = typeof EventSource === 'undefined' ? undefined : new EventSource('/api/v1/events')
    source?.addEventListener('workflow', () => void refresh())
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', onVisible)
      document.removeEventListener('visibilitychange', onVisible)
      source?.close()
    }
  }, [])

  // R7：候选人变化只增量刷新候选人详情与列表，不再触发 bootstrap/jobs 全量重拉；
  // dashboard 计数由统一轮询自然收敛。
  const refreshCandidateList = async () => {
    try { setCandidates((await api.allCandidates()).items) } catch { /* 列表刷新失败时保留现状，下一轮再试。 */ }
  }

  // 合并双轮询：单一定时器统一刷新 dashboard + 候选人详情，避免两个独立 setInterval(2000)。
  useEffect(() => {
    let active = true
    let refreshingDashboard = false
    let refreshingCandidate = false
    const tick = async () => {
      if (!active || document.hidden) return
      // 刷新 dashboard
      if (!refreshingDashboard) {
        refreshingDashboard = true
        try {
          const next = await api.dashboard()
          if (active) setDashboard(next)
        } catch {
          // The full bootstrap path surfaces persistent connection failures.
        } finally {
          refreshingDashboard = false
        }
      }
      // 刷新候选人详情（当详情面板打开时）
      const cid = candidateStateRef.current?.id
      if (cid && !refreshingCandidate) {
        refreshingCandidate = true
        try {
          const fresh = (await api.candidate(cid)).candidate
          if (!active || candidateStateRef.current?.id !== cid) return
          const current = candidateStateRef.current
          const changed = !current
            || current.updated_at !== fresh.updated_at
            || current.clean_stage !== fresh.clean_stage
            || current.raw_status !== fresh.raw_status
            || current.events.length !== fresh.events.length
          if (changed) {
            candidateStateRef.current = fresh
            setCandidate(fresh)
            void refreshCandidateList()
          }
        } catch {
          // Keep the current detail visible during a transient core restart.
        } finally {
          refreshingCandidate = false
        }
      }
    }
    const timer = window.setInterval(tick, 2000)
    const onVisible = () => { if (!document.hidden) void tick() }
    window.addEventListener('focus', tick)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', tick)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  useEffect(() => { candidateStateRef.current = candidate }, [candidate])

  const openCandidate = async (id: number) => {
    try { setJob(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setCandidate((await api.candidate(id)).candidate); location.hash = `candidate=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const refreshCandidateDetail = async (id: number) => {
    const fresh = (await api.candidate(id)).candidate
    candidateStateRef.current = fresh
    setCandidate(fresh)
    await refreshCandidateList()
  }
  const openJob = async (id: number) => {
    try { setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setJob((await api.job(id)).job); setTab('jobs'); location.hash = `job=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const openWorkflow = async (id: string) => {
    try {
      setJob(undefined); setCandidate(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
      const resolved = await resolveWorkflowRevision(id, api.workflow)
      setWorkflow(resolved.value)
      const nextHash = `workflow=${encodeURIComponent(resolved.id)}`
      if (resolved.id !== id) history.replaceState(null, '', `${location.pathname}${location.search}#${nextHash}`)
      else location.hash = nextHash
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const openSourcingCandidates = (workflowId: string) => {
    setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId('')
    setSourcingCandidatesWorkflowId(workflowId)
    const nextHash = `sourcing_candidates=${encodeURIComponent(workflowId)}`
    if (location.hash.slice(1) !== nextHash) history.pushState(null, '', `${location.pathname}${location.search}#${nextHash}`)
  }
  const openAnalysis = async (id: string, templateId = '') => {
    try {
      setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setSourcingCandidatesWorkflowId('')
      const run = await api.analysisRun(id)
      const resolvedTemplateId = templateId || run.template_id || ''
      const trend = resolvedTemplateId ? await api.analyticsTemplateTrend(resolvedTemplateId).catch(() => undefined) : undefined
      setAnalysis(run.result); setAnalysisTemplateId(resolvedTemplateId); setAnalysisTrend(trend)
      const nextHash = `analysis=${encodeURIComponent(id)}`
      if (location.hash.slice(1) !== nextHash) history.pushState(null, '', `${location.pathname}${location.search}#${nextHash}`)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  useEffect(() => {
    const openHash = () => {
      const hash = new URLSearchParams(location.hash.slice(1))
      const analysisId = hash.get('analysis')
      const candidateId = Number(hash.get('candidate'))
      const workflowId = hash.get('workflow')
      const jobId = Number(hash.get('job'))
      const sourcingCandidatesId = hash.get('sourcing_candidates')
      if (sourcingCandidatesId) { openSourcingCandidates(sourcingCandidatesId); return }
      if (analysisId) { void openAnalysis(analysisId); return }
      if (candidateId) { void openCandidate(candidateId); return }
      if (workflowId) { void openWorkflow(workflowId); return }
      if (jobId) { void openJob(jobId); return }
      setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
    }
    queueMicrotask(openHash)
    addEventListener('hashchange', openHash)
    return () => removeEventListener('hashchange', openHash)
  }, [])
  const runTemplate = async (id: string) => {
    setAnalysisBusy(id)
    try {
      const result = (await api.runAnalyticsTemplate(id)).result
      const trend = await api.analyticsTemplateTrend(id).catch(() => undefined)
      setAnalysis(result); setAnalysisTemplateId(id); setAnalysisTrend(trend)
      history.pushState(null, '', `${location.pathname}${location.search}#analysis=${encodeURIComponent(result.run_id)}`)
      await refreshWorkbench()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setAnalysisBusy('') }
  }
  const refreshAnalysis = async () => {
    if (!analysis) return
    setAnalysisBusy('refresh')
    try {
      const result = analysisTemplateId
        ? (await api.runAnalyticsTemplate(analysisTemplateId)).result
        : (await api.refreshAnalysis(analysis.run_id)).result
      const trend = analysisTemplateId ? await api.analyticsTemplateTrend(analysisTemplateId).catch(() => undefined) : undefined
      setAnalysis(result); setAnalysisTrend(trend)
      history.replaceState(null, '', `${location.pathname}${location.search}#analysis=${encodeURIComponent(result.run_id)}`)
      await refreshWorkbench()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setAnalysisBusy('') }
  }
  const exportAnalysis = async () => {
    if (!analysis) return
    setAnalysisBusy('export')
    try {
      const result = await api.exportAnalysis(analysis.run_id)
      const link = document.createElement('a')
      link.href = result.artifact.download_url
      link.download = ''
      link.click()
      setNotice(`已导出分析报告：${result.artifact.title}`)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setAnalysisBusy('') }
  }
  const saveTemplate = async (input: AnalysisTemplateInput) => {
    setAnalysisBusy('template-save')
    try {
      if (templateDialog && templateDialog !== 'new') await api.updateAnalyticsTemplate(templateDialog.template_id, input)
      else await api.createAnalyticsTemplate(input)
      setTemplateDialog(undefined)
      await refreshWorkbench()
      setNotice(templateDialog === 'new' ? '固定分析已创建' : '固定分析已更新')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setAnalysisBusy('') }
  }
  const deleteTemplate = async () => {
    if (!templateDialog || templateDialog === 'new') return
    setAnalysisBusy('template-save')
    try {
      await api.deleteAnalyticsTemplate(templateDialog.template_id)
      setTemplateDialog(undefined)
      await refreshWorkbench()
      setNotice('固定分析已删除')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setAnalysisBusy('') }
  }
  const closeOverlay = () => { setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId(''); history.replaceState(null, '', `${location.pathname}${location.search}`) }
  const workflowContext = workflow?.goal.context as Record<string, unknown> | undefined
  const workflowJobId = Number(workflowContext?.id || 0)
  const workflowJob = jobs.find(item => item.id === workflowJobId)
  const activeContext = analysis
    ? { type: 'page', page: tab, mode: 'analysis_review', analysis_id: analysis.run_id }
    : candidate
    ? { type: 'candidate', id: candidate.id, candidate: candidate.name, client: candidate.client, job: candidate.job, mode: 'candidate_review', page: tab }
    : job
      ? { type: 'job', id: job.id, client: job.client, job: job.title, mode: 'job_review', page: tab }
      : workflow
        ? {
            type: 'workflow', id: workflow.workflow.workflow_id,
            client: String(workflowContext?.client || workflowJob?.client || ''),
            job: String(workflowContext?.job || workflowJob?.title || ''),
            mode: String(workflowContext?.mode || 'workflow_review'),
            page: tab,
          }
        : sourcingCandidatesWorkflowId
          ? { type: 'page', page: tab, mode: 'page_review' }
          : { type: 'page', page: tab, mode: 'page_review' }
  const showAgent = useCallback((context: AgentContext) => {
    setAgentContext(context); setTab('agent'); setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
    history.replaceState(null, '', `${location.pathname}${location.search}`)
  }, [])
  const openAgent = (context: AgentContext = activeContext) => showAgent(context)
  useEffect(() => {
    const navigate = (event: Event) => showAgent((event as CustomEvent<AgentContext>).detail)
    window.addEventListener(AGENT_NAVIGATE_EVENT, navigate)
    return () => window.removeEventListener(AGENT_NAVIGATE_EVENT, navigate)
  }, [showAgent])

  if (error && !boot) return <Diagnostics error={error} retry={() => { setError(''); setRefreshKey(x => x + 1) }} />

  const pageTitle = sourcingCandidatesWorkflowId ? '寻访候选人名单' : analysis ? '分析结果' : tabs.find(x => x[0] === tab)?.[1]
  const navigateTab = (id: Tab) => {
    setTab(id); setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
    history.replaceState(null, '', `${location.pathname}${location.search}`)
  }

  const handleWorkbenchAction = (item: WorkbenchItem) => {
    const action = item.primary_action
    if (action.type === 'open_candidate') void openCandidate(Number(action.id))
    else if (action.type === 'open_workflow') void openWorkflow(action.id)
    else if (action.type === 'open_analysis') void openAnalysis(action.id)
  }
  const openAgentObject = (reference: AgentReference) => {
    if (reference.type === 'candidate' || reference.type === 'job_candidate') void openCandidate(Number(reference.id))
    else if (reference.type === 'job') {
      void api.job(Number(reference.id)).then(result => setJob(result.job)).catch(error => setError(error instanceof Error ? error.message : String(error)))
    } else if (reference.type === 'workflow') void openWorkflow(String(reference.id))
  }

  return <div className={`shell ${tab === 'agent' && !analysis ? 'agent-mode' : ''}`}>
    <aside className="nav">
      <div className="brand"><span>ASA</span><strong>Agent</strong><small>Recruiting Workbench</small></div>
      <nav>{tabs.map(([id, label, icon]) => <button key={id} className={!analysis && tab === id ? 'active' : ''} onClick={() => navigateTab(id)}>{icon}<span>{label}</span></button>)}</nav>
      <div className="nav-status"><Wifi/><span>Core 在线</span></div>
      <a className="legacy" href="/admin/legacy" target="_blank">审计与旧版</a>
    </aside>
    <main className="main">
      <header className="topbar">
        <div><h1>{pageTitle}</h1><p>{boot?.core?.status === 'connected' ? 'ASA Agent 在线 · Core 已连接 · v3 实时数据' : 'ASA Agent 正在连接 Core'}</p></div>
        {!analysis && tab !== 'agent' && <button className="button copilot-launch" title="交给 Agent" aria-label="交给 Agent" onClick={()=>openAgent(activeContext)}><MessageSquareText/><span>Agent</span></button>}
      </header>
      {coreOffline && <div className="core-offline-banner" role="alert"><span>ASA Core 连接中断，检查本机服务后可点击重连</span><button className="button" onClick={() => void probeCore()}>重连</button></div>}
      <div className="content">
        {sourcingCandidatesWorkflowId && <SourcingCandidatesPage workflowId={sourcingCandidatesWorkflowId} onBack={closeOverlay} onOpenCandidate={openCandidate} />}
        {!sourcingCandidatesWorkflowId && analysis && <AnalysisWorkspace result={analysis} trend={analysisTrend} busy={analysisBusy === 'refresh' || analysisBusy === 'export' ? analysisBusy : undefined} close={closeOverlay} refresh={() => void refreshAnalysis()} exportReport={() => void exportAnalysis()} />}
        {!sourcingCandidatesWorkflowId && !analysis && tab === 'agent' && <AgentWorkspace dashboard={dashboard} jobs={jobs} workbench={workbench || emptyWorkbench} templates={templates} context={agentContext} onOpenAnalysis={id => void openAnalysis(id)} onRunTemplate={id => void runTemplate(id)} onManageTemplate={setTemplateDialog} onCreateTemplate={() => setTemplateDialog('new')} onWorkbenchAction={handleWorkbenchAction} onOpenFullObject={openAgentObject} />}
        {!sourcingCandidatesWorkflowId && !analysis && tab === 'jobs' && <Jobs items={jobs} onSelect={openJob} />}
        {!sourcingCandidatesWorkflowId && !analysis && tab === 'progress' && <Progress items={candidates} openCandidate={openCandidate} />}
        {!sourcingCandidatesWorkflowId && !analysis && tab === 'candidates' && <Candidates items={candidates} openCandidate={openCandidate} />}
      </div>
    </main>
    {(tab !== 'agent' || analysis) && <aside className="context-rail">
      {analysis ? <>
        <section><header><Activity/><h2>分析范围</h2></header><dl>{Object.entries(analysis.scope).map(([key, value]) => <div key={key}><dt>{analysisScopeLabels[key] || key.replaceAll('_', ' ')}</dt><dd>{analysisScopeValue(key, value)}</dd></div>)}{!Object.keys(analysis.scope).length && <div><dt>范围</dt><dd>全部业务数据</dd></div>}</dl></section>
        <section><header><Database/><h2>数据口径</h2></header><p>数据截至 {analysisTime(analysis.data_as_of)}</p><p>口径版本 {analysis.catalog_version}</p>{analysis.truncated && <span className="rail-warning">结果已截断</span>}</section>
        <section><header><Pin/><h2>证据引用</h2></header><div className="rail-links">{analysis.references.map(reference => <a key={`${reference.type}:${reference.id}`} href={reference.href}>{reference.label}</a>)}{!analysis.references.length && <p>当前分析无对象引用</p>}</div></section>
        {!!analysis.caveats.length && <section><header><ShieldAlert/><h2>数据提示</h2></header>{analysis.caveats.map(item => <p key={item}>{item}</p>)}</section>}
      </> : <>
        <section><header><Activity/><h2>今日节奏</h2></header><dl><div><dt>待处理</dt><dd>{workbench?.summary?.pending ?? dashboard?.counts?.pending_candidates ?? '-'}</dd></div><div><dt>运行中</dt><dd>{workbench?.summary?.running ?? '-'}</dd></div><div><dt>已交付</dt><dd>{workbench?.summary?.delivered ?? '-'}</dd></div></dl></section>
        <section><header><Pin/><h2>固定分析</h2></header><div className="rail-links">{templates.slice(0, 6).map(item => <button key={item.template_id} onClick={() => void runTemplate(item.template_id)}>{item.name}</button>)}{!templates.length && <p>暂无固定分析</p>}</div></section>
        <section><header><Database/><h2>数据连接</h2></header><p>v3 统一库</p><span className="rail-online"><i/>实时连接</span></section>
      </>}
    </aside>}
    {job && <JobPanel value={job} close={closeOverlay} openCandidate={openCandidate} />}
    {candidate && <CandidatePanel value={candidate} close={closeOverlay} changed={() => refreshCandidateDetail(candidate.id)} />}
    {workflow && <WorkflowPanel value={workflow} jobs={jobs} close={closeOverlay} reload={() => openWorkflow(workflow.workflow.workflow_id)} openCandidate={openCandidate} archived={() => { closeOverlay(); setRefreshKey(value => value + 1) }} />}
    {templateDialog && <AnalysisTemplateDialog catalogs={analysisCatalog} template={templateDialog === 'new' ? undefined : templateDialog} busy={analysisBusy === 'template-save'} onCancel={() => setTemplateDialog(undefined)} onSave={saveTemplate} onDelete={templateDialog === 'new' ? undefined : deleteTemplate} />}
    {error && <div className="toast"><ShieldAlert/> {error}<button onClick={() => setError('')}><X/></button></div>}
    {notice && <div className="toast success"><Database/> {notice}<button onClick={() => setNotice('')}><X/></button></div>}
  </div>
}
