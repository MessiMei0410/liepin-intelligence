import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { Activity, Database, LoaderCircle, MessageSquareText, Pin, ShieldAlert, Wifi, X } from 'lucide-react'
import { api, AnalysisCatalogItem, AnalysisResult, AnalysisTemplate, AnalysisTemplateInput, AnalysisTrend, Bootstrap, Candidate, CandidateDetail, Dashboard, Job, JobDetail, Workbench, WorkbenchItem, Workflow } from '../api'
import { Tab, tabs } from '../shared/tabs'
import { Jobs } from '../pages/Jobs'
import { Candidates } from '../pages/Candidates'
import { Progress } from '../pages/Progress'
import { resolveWorkflowRevision } from '../workflow/workflowRevision'
import { AgentWorkspace } from '../agent/AgentWorkspace'
import { AGENT_NAVIGATE_EVENT, notifyFullObjectClosed } from '../agent/navigation'
import type { AgentContext, AgentReference } from '../agent/transport'
import { useGlobalDialogDrag } from '../shared/useGlobalDialogDrag'
import { isBareDetached } from '../shared/nativeBridge'
import { BareCandidateList } from '../agent/BareCandidateList'
import { CANDIDATE_UPDATED_EVENT, type CandidateUpdatedDetail } from '../shared/candidateEvents'
import { LeaveConfirmDialog } from '../components/LeaveConfirmDialog'
import { hasDirtyForms, subscribeDirtyForms } from '../shared/dirtyForm'

// 浮层/次屏组件按需懒加载（P2-2）：四个主 tab 首屏保持直出，点击打开面板时才拉取对应 chunk
const JobPanel = lazy(() => import('../panels/JobPanel').then(module => ({ default: module.JobPanel })))
const CandidatePanel = lazy(() => import('../panels/CandidatePanel').then(module => ({ default: module.CandidatePanel })))
const WorkflowSurface = lazy(() => import('../workflows/WorkflowSurface').then(module => ({ default: module.WorkflowSurface })))
const SourcingCandidatesPage = lazy(() => import('../pages/SourcingCandidatesPage').then(module => ({ default: module.SourcingCandidatesPage })))
const AnalysisWorkspace = lazy(() => import('../pages/AnalysisWorkspace').then(module => ({ default: module.AnalysisWorkspace })))
const AnalysisTemplateDialog = lazy(() => import('../components/AnalysisTemplateDialog').then(module => ({ default: module.AnalysisTemplateDialog })))
const Diagnostics = lazy(() => import('./Diagnostics').then(module => ({ default: module.Diagnostics })))

const panelFallback = <div className="empty"><LoaderCircle className="spin"/>面板加载中…</div>

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

/** hash 中的 tab 参数只接受四个主入口，非法值忽略（保持当前 tab）。 */
const tabFromHashValue = (value: string | null): Tab | undefined => {
  if (!value) return undefined
  return tabs.some(([id]) => id === value) ? (value as Tab) : undefined
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
  const [bareList, setBareList] = useState(false)
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
  // 丢弃型导航守卫：存在未提交表单时先弹确认（React 对话框，禁原生 confirm）。
  const [pendingLeave, setPendingLeave] = useState<() => void>()
  const [dirtyCount, setDirtyCount] = useState(0)
  const candidateStateRef = useRef<CandidateDetail | undefined>(undefined)
  const workflowStateRef = useRef<Workflow | undefined>(undefined)
  const jobStateRef = useRef<JobDetail | undefined>(undefined)
  const coreFailuresRef = useRef(0)
  const workbenchRefreshRef = useRef(0)
  const jobsRefreshRef = useRef(0)
  const candidatesRefreshRef = useRef(0)
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

  const refreshWorkbench = useCallback(async () => {
    const refreshId = ++workbenchRefreshRef.current
    await Promise.allSettled([
      api.workbench().then(next => {
        if (refreshId === workbenchRefreshRef.current && Array.isArray(next.items) && next.summary) setWorkbench(next)
      }),
      api.analyticsTemplates().then(next => {
        if (refreshId === workbenchRefreshRef.current && Array.isArray(next.items)) setTemplates(next.items)
      }),
    ])
  }, [])

  useEffect(() => {
    let active = true
    const jobsRefreshId = ++jobsRefreshRef.current
    const candidatesRefreshId = ++candidatesRefreshRef.current
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
        if (jobsResult.status === 'fulfilled') {
          if (jobsRefreshId === jobsRefreshRef.current) setJobs(jobsResult.value.items)
        } else failures.push(`岗位看板：${String(jobsResult.reason?.message || jobsResult.reason)}`)
        if (candidatesResult.status === 'fulfilled') {
          if (candidatesRefreshId === candidatesRefreshRef.current) setCandidates(candidatesResult.value.items)
        } else failures.push(`人选模块：${String(candidatesResult.reason?.message || candidatesResult.reason)}`)
        if (failures.length) setError(`部分模块加载失败。${failures.join('；')}`)
        else setError('')
      })
    queueMicrotask(() => void refreshWorkbench())
    queueMicrotask(() => {
      void api.analyticsCatalog().then(result => setAnalysisCatalog(result.items)).catch(() => undefined)
    })
    return () => { active = false }
  }, [refreshKey, refreshWorkbench])

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
  }, [refreshWorkbench])

  // R7：候选人变化只增量刷新候选人详情与列表，不再触发 bootstrap/jobs 全量重拉；
  // dashboard 计数由统一轮询自然收敛。
  const refreshCandidateList = useCallback(async () => {
    const refreshId = ++candidatesRefreshRef.current
    try {
      const result = await api.allCandidates()
      if (refreshId === candidatesRefreshRef.current) setCandidates(result.items)
    } catch { /* 列表刷新失败时保留现状，下一轮再试。 */ }
  }, [])

  const refreshCreatedCandidate = useCallback(async (candidateId: number) => {
    const refreshId = ++candidatesRefreshRef.current
    await Promise.allSettled([
      api.candidate(candidateId).then(result => {
        if (refreshId !== candidatesRefreshRef.current) return
        setCandidates(current => [result.candidate, ...current.filter(candidate => candidate.id !== result.candidate.id)])
      }),
      api.allCandidates().then(result => {
        if (refreshId === candidatesRefreshRef.current) setCandidates(result.items)
      }),
    ])
  }, [])

  // Mapping 等入口新建岗位关系后，立即让四主 tab 的岗位/人选数据回读数据库。
  // 普通阶段更新仍走详情增量刷新，不触发这条全量列表路径。
  useEffect(() => {
    const onCandidateUpdated = (event: Event) => {
      const detail = (event as CustomEvent<CandidateUpdatedDetail>).detail
      if (!detail?.created) return
      const jobsRefreshId = ++jobsRefreshRef.current
      void Promise.allSettled([
        refreshCreatedCandidate(detail.id),
        api.allJobs().then(result => {
          if (jobsRefreshId === jobsRefreshRef.current) setJobs(result.items)
        }),
        refreshWorkbench(),
      ])
    }
    window.addEventListener(CANDIDATE_UPDATED_EVENT, onCandidateUpdated)
    return () => window.removeEventListener(CANDIDATE_UPDATED_EVENT, onCandidateUpdated)
  }, [refreshCreatedCandidate, refreshWorkbench])

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
  }, [refreshCandidateList])

  useEffect(() => { candidateStateRef.current = candidate }, [candidate])
  useEffect(() => { workflowStateRef.current = workflow }, [workflow])
  useEffect(() => { jobStateRef.current = job }, [job])
  useEffect(() => subscribeDirtyForms(setDirtyCount), [])

  // 压入一条视图 hash（tab 或对象）。相同 hash 不重复入栈，避免连点产生死历史条目。
  const pushHash = (next: string) => {
    if (location.hash.slice(1) === next) return
    history.pushState(null, '', `${location.pathname}${location.search}#${next}`)
  }

  // 丢弃型导航统一走守卫：干净时立即执行，有未提交表单时先确认再执行。
  const guardedNavigate = (action: () => void) => {
    if (hasDirtyForms()) setPendingLeave(() => action)
    else action()
  }
  const confirmLeave = () => {
    const action = pendingLeave
    setPendingLeave(undefined)
    action?.()
  }

  const openCandidate = async (id: number) => {
    try { setJob(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setBareList(false); setCandidate((await api.candidate(id)).candidate); pushHash(`candidate=${id}${isBareDetached() ? '&bare=1' : ''}`) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const refreshCandidateDetail = async (id: number) => {
    const fresh = (await api.candidate(id)).candidate
    candidateStateRef.current = fresh
    setCandidate(fresh)
    await refreshCandidateList()
  }
  const openJob = async (id: number) => {
    try { setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setBareList(false); setJob((await api.job(id)).job); setTab('jobs'); pushHash(`job=${id}${isBareDetached() ? '&bare=1' : ''}`) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const refreshJobDetail = async (id: number) => {
    const fresh = (await api.job(id)).job
    setJob(current => current?.id === id ? fresh : current)
  }
  const openWorkflow = async (id: string) => {
    try {
      setJob(undefined); setCandidate(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId(''); setBareList(false)
      const resolved = await resolveWorkflowRevision(id, api.workflow)
      workflowStateRef.current = resolved.value
      setWorkflow(resolved.value)
      const nextHash = `workflow=${encodeURIComponent(resolved.id)}${isBareDetached() ? '&bare=1' : ''}`
      // 打开对象压入历史（返回键可回到上一视图）；revision 规范化或重复打开时原位替换，不新增条目。
      if (resolved.id !== id || location.hash.slice(1) === nextHash) history.replaceState(null, '', `${location.pathname}${location.search}#${nextHash}`)
      else pushHash(nextHash)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const refreshWorkflowDetail = async (id: string) => {
    const resolved = await resolveWorkflowRevision(id, api.workflow)
    if (workflowStateRef.current?.workflow.workflow_id !== id) return
    workflowStateRef.current = resolved.value
    setWorkflow(resolved.value)
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
      const candidateList = hash.get('candidate_list')
      const tabId = tabFromHashValue(hash.get('tab'))
      if (candidateList) { setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId(''); setBareList(true); return }
      setBareList(false)
      if (tabId) setTab(tabId)
      if (sourcingCandidatesId) { openSourcingCandidates(sourcingCandidatesId); return }
      if (analysisId) { void openAnalysis(analysisId); return }
      if (candidateId) { void openCandidate(candidateId); return }
      if (workflowId) { void openWorkflow(workflowId); return }
      if (jobId) { void openJob(jobId); return }
      // 回到无对象的 tab 视图（多为浏览器返回）：清空 overlay，并通知名单暂存恢复。
      const hadFullObject = Boolean(candidateStateRef.current || workflowStateRef.current || jobStateRef.current)
      setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
      if (hadFullObject) notifyFullObjectClosed()
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
  const closeOverlay = () => {
    const close = () => {
      // 纯净模式（独立窗口）：优先退回上一页（如从名单点进详情），无历史再清空。
      if (isBareDetached() && history.length > 1) { history.back(); return }
      workflowStateRef.current = undefined
      setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId(''); setBareList(false)
      // 关闭后回到当前 tab 的基础视图（replace 不新增历史；返回键仍可回到更早的对象视图）。
      history.replaceState(null, '', `${location.pathname}${location.search}#tab=${tab}`)
      notifyFullObjectClosed()
    }
    guardedNavigate(close)
  }
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
    // 交给 Agent 也压入历史：从 Agent 按返回可回到先前的对象/页面视图（不再是单程门）。
    const enter = () => {
      setAgentContext(context); setTab('agent'); setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
      if (location.hash.slice(1) !== 'tab=agent') history.pushState(null, '', `${location.pathname}${location.search}#tab=agent`)
    }
    if (hasDirtyForms()) setPendingLeave(() => enter)
    else enter()
  }, [])
  const openAgent = (context: AgentContext = activeContext) => showAgent(context)
  useEffect(() => {
    const navigate = (event: Event) => showAgent((event as CustomEvent<AgentContext>).detail)
    window.addEventListener(AGENT_NAVIGATE_EVENT, navigate)
    return () => window.removeEventListener(AGENT_NAVIGATE_EVENT, navigate)
  }, [showAgent])

  // 错误/通知 toast 8 秒后自动消失，避免常驻遮挡。
  useEffect(() => {
    if (!error) return
    const timer = window.setTimeout(() => setError(''), 8000)
    return () => window.clearTimeout(timer)
  }, [error])
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 8000)
    return () => window.clearTimeout(timer)
  }, [notice])

  if (error && !boot) return <Suspense fallback={<div className="empty"><LoaderCircle className="spin"/>诊断页加载中…</div>}><Diagnostics error={error} retry={() => { setError(''); setRefreshKey(x => x + 1) }} /></Suspense>

  const pageTitle = sourcingCandidatesWorkflowId ? '寻访候选人名单' : analysis ? '分析结果' : tabs.find(x => x[0] === tab)?.[1]
  const todayLabel = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' }).format(new Date())
  // 导航角标：岗位看板显示开放岗位数、人选进度显示待处理数，其余 tab 无角标。
  const tabBadge = (id: Tab) => {
    if (id === 'jobs') {
      const count = dashboard?.counts?.active_jobs
      return count ? <em className="nav-badge" aria-label={`${count} 个开放岗位`}>{count > 99 ? '99+' : count}</em> : null
    }
    if (id === 'progress') {
      const count = dashboard?.counts?.pending_candidates
      return count ? <em className="nav-badge" aria-label={`${count} 个待处理人选`}>{count > 99 ? '99+' : count}</em> : null
    }
    return null
  }
  const navigateTab = (id: Tab) => {
    guardedNavigate(() => {
      workflowStateRef.current = undefined
      setTab(id); setJob(undefined); setCandidate(undefined); setWorkflow(undefined); setAnalysis(undefined); setAnalysisTrend(undefined); setAnalysisTemplateId(''); setSourcingCandidatesWorkflowId('')
      // tab 进 hash：刷新/分享后恢复当前入口；push 而非 replace，返回键可回上一 tab。
      pushHash(`tab=${id}`)
    })
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

  // 纯净模式（独立窗口，hash 带 bare=1）：只渲染目标页面，不带 Agent 主界面/导航/侧栏。
  if (isBareDetached()) {
    return <div className="shell bare-shell">
      {bareList && <BareCandidateList onOpenCandidate={id => void openCandidate(id)} />}
      <Suspense fallback={panelFallback}>
        {job && <JobPanel value={job} close={closeOverlay} openCandidate={openCandidate} changed={() => refreshJobDetail(job.id)} />}
        {candidate && <CandidatePanel value={candidate} close={closeOverlay} changed={() => refreshCandidateDetail(candidate.id)} />}
        {workflow && <WorkflowSurface value={workflow} jobs={jobs} close={closeOverlay} reload={() => refreshWorkflowDetail(workflow.workflow.workflow_id)} openCandidate={openCandidate} archived={closeOverlay} />}
      </Suspense>
      {!bareList && !job && !candidate && !workflow && !error && <div className="bare-empty" role="status">页面已关闭，可直接关闭此窗口。</div>}
      {pendingLeave && <LeaveConfirmDialog dirtyCount={dirtyCount} onConfirm={confirmLeave} onCancel={() => setPendingLeave(undefined)} />}
      {error && <div className="toast"><ShieldAlert/> {error}<button onClick={() => setError('')}><X/></button></div>}
    </div>
  }

  return <div className={`shell ${tab === 'agent' && !analysis ? 'agent-mode' : ''}`}>
    <aside className="nav">
      <div className="brand"><span>ASA</span><strong>Agent</strong><small>Recruiting Workbench</small></div>
      <nav>{tabs.map(([id, label, icon]) => <button key={id} className={!analysis && tab === id ? 'active' : ''} onClick={() => navigateTab(id)}>{icon}<span>{label}</span>{tabBadge(id)}</button>)}</nav>
      <div className="nav-status"><Wifi/><span>Core 在线</span></div>
      <a className="legacy" href="/admin/legacy" target="_blank">审计与旧版</a>
    </aside>
    <main className="main">
      <header className="topbar">
        <div><h1>{pageTitle}</h1><p>{boot?.core?.status === 'connected' ? <>{todayLabel} · ASA Agent 在线 · Core 已连接<span className="topbar-extra"> · v3 实时数据</span></> : 'ASA Agent 正在连接 Core'}</p></div>
        {!analysis && tab !== 'agent' && <button className="button copilot-launch" title="交给 Agent" aria-label="交给 Agent" onClick={()=>openAgent(activeContext)}><MessageSquareText/><span>Agent</span></button>}
      </header>
      {coreOffline && <div className="core-offline-banner" role="alert"><span>ASA Core 连接中断，检查本机服务后可点击重连</span><button className="button" onClick={() => void probeCore()}>重连</button></div>}
      <div className="content">
        <Suspense fallback={<div className="empty"><LoaderCircle className="spin"/>页面加载中…</div>}>
          {sourcingCandidatesWorkflowId && <SourcingCandidatesPage workflowId={sourcingCandidatesWorkflowId} onBack={closeOverlay} onOpenCandidate={openCandidate} />}
          {!sourcingCandidatesWorkflowId && analysis && <AnalysisWorkspace result={analysis} trend={analysisTrend} busy={analysisBusy === 'refresh' || analysisBusy === 'export' ? analysisBusy : undefined} close={closeOverlay} refresh={() => void refreshAnalysis()} exportReport={() => void exportAnalysis()} />}
        </Suspense>
        {!sourcingCandidatesWorkflowId && !analysis && tab === 'agent' && <AgentWorkspace jobs={jobs} workbench={workbench || emptyWorkbench} templates={templates} context={agentContext} templateBusyId={analysisBusy} onOpenAnalysis={id => void openAnalysis(id)} onRunTemplate={id => void runTemplate(id)} onManageTemplate={setTemplateDialog} onCreateTemplate={() => setTemplateDialog('new')} onWorkbenchAction={handleWorkbenchAction} onOpenFullObject={openAgentObject} />}
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
        <section><header><Pin/><h2>固定分析</h2></header><div className="rail-links">{templates.slice(0, 6).map(item => { const busy = analysisBusy === item.template_id; return <button key={item.template_id} disabled={busy} onClick={() => void runTemplate(item.template_id)}>{busy ? `${item.name} · 运行中…` : item.name}</button> })}{!templates.length && <p>暂无固定分析</p>}</div></section>
        <section><header><Database/><h2>数据连接</h2></header><p>v3 统一库</p><span className="rail-online"><i/>实时连接</span></section>
      </>}
    </aside>}
    <Suspense fallback={panelFallback}>
      {job && <JobPanel value={job} close={closeOverlay} openCandidate={openCandidate} changed={() => refreshJobDetail(job.id)} />}
      {candidate && <CandidatePanel value={candidate} close={closeOverlay} changed={() => refreshCandidateDetail(candidate.id)} />}
      {workflow && <WorkflowSurface value={workflow} jobs={jobs} close={closeOverlay} reload={() => refreshWorkflowDetail(workflow.workflow.workflow_id)} openCandidate={openCandidate} archived={() => { closeOverlay(); setRefreshKey(value => value + 1) }} />}
      {templateDialog && <AnalysisTemplateDialog catalogs={analysisCatalog} template={templateDialog === 'new' ? undefined : templateDialog} busy={analysisBusy === 'template-save'} onCancel={() => setTemplateDialog(undefined)} onSave={saveTemplate} onDelete={templateDialog === 'new' ? undefined : deleteTemplate} />}
      {pendingLeave && <LeaveConfirmDialog dirtyCount={dirtyCount} onConfirm={confirmLeave} onCancel={() => setPendingLeave(undefined)} />}
    </Suspense>
    {error && <div className="toast"><ShieldAlert/> {error}<button onClick={() => setError('')}><X/></button></div>}
    {notice && <div className="toast success"><Database/> {notice}<button onClick={() => setNotice('')}><X/></button></div>}
  </div>
}
