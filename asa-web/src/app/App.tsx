import { useEffect, useRef, useState } from 'react'
import { MessageSquareText, Search, ShieldAlert, X } from 'lucide-react'
import { api, Bootstrap, Candidate, CandidateDetail, Dashboard, Job, JobDetail, Workflow } from '../api'
import { Tab, tabs } from '../shared/tabs'
import { humanizeActionError } from '../shared/errors'
import { publishCopilotContext, openCopilotWindow } from '../copilot/bridge'
import { Overview } from '../pages/Overview'
import { Jobs } from '../pages/Jobs'
import { Candidates } from '../pages/Candidates'
import { Progress } from '../pages/Progress'
import { RadarPage } from '../pages/Radar'
import { JobPanel } from '../panels/JobPanel'
import { CandidatePanel } from '../panels/CandidatePanel'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { Diagnostics } from './Diagnostics'

export function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [boot, setBoot] = useState<Bootstrap>()
  const [dashboard, setDashboard] = useState<Dashboard>()
  const [jobs, setJobs] = useState<Job[]>([])
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [query, setQuery] = useState('')
  const [job, setJob] = useState<JobDetail>()
  const [candidate, setCandidate] = useState<CandidateDetail>()
  const [workflow, setWorkflow] = useState<Workflow>()
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const candidateStateRef = useRef<CandidateDetail | undefined>(undefined)

  useEffect(() => {
    Promise.all([api.bootstrap(), api.dashboard(), api.jobs(), api.candidates()])
      .then(([b, d, j, c]) => { setBoot(b); setDashboard(d); setJobs(j.items); setCandidates(c.items); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [refreshKey])

  useEffect(() => {
    let active = true
    let refreshing = false
    const refreshDashboard = async () => {
      if (!active || refreshing || document.hidden) return
      refreshing = true
      try {
        const next = await api.dashboard()
        if (active) setDashboard(next)
      } catch {
        // The full bootstrap path surfaces persistent connection failures.
      } finally {
        refreshing = false
      }
    }
    const timer = window.setInterval(refreshDashboard, 2000)
    const onVisible = () => { if (!document.hidden) void refreshDashboard() }
    window.addEventListener('focus', refreshDashboard)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshDashboard)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  useEffect(() => { candidateStateRef.current = candidate }, [candidate])

  // R7：候选人变化只增量刷新候选人详情与列表，不再触发 bootstrap/jobs 全量重拉；
  // dashboard 计数由既有 2s 轮询自然收敛。
  const refreshCandidateList = async () => {
    try { setCandidates((await api.candidates()).items) } catch { /* 列表刷新失败时保留现状，下一轮再试。 */ }
  }

  useEffect(() => {
    const candidateId = candidate?.id
    if (!candidateId) return
    let active = true
    const refreshCandidate = async () => {
      try {
        const fresh = (await api.candidate(candidateId)).candidate
        if (!active || candidateStateRef.current?.id !== candidateId) return
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
      }
    }
    const timer = window.setInterval(refreshCandidate, 2000)
    window.addEventListener('focus', refreshCandidate)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshCandidate)
    }
  }, [candidate?.id])

  useEffect(() => {
    const openHash = () => {
      const hash = new URLSearchParams(location.hash.slice(1))
      const id = Number(hash.get('candidate'))
      const workflowId = hash.get('workflow')
      const jobId = Number(hash.get('job'))
      if (id) { openCandidate(id); return }
      if (workflowId) { openWorkflow(workflowId); return }
      if (jobId) { openJob(jobId); return }
      setJob(undefined); setCandidate(undefined); setWorkflow(undefined)
    }
    openHash(); addEventListener('hashchange', openHash); return () => removeEventListener('hashchange', openHash)
  }, [])

  const openCandidate = async (id: number) => {
    try { setJob(undefined); setWorkflow(undefined); setCandidate((await api.candidate(id)).candidate); location.hash = `candidate=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const refreshCandidateDetail = async (id: number) => {
    const fresh = (await api.candidate(id)).candidate
    candidateStateRef.current = fresh
    setCandidate(fresh)
    await refreshCandidateList()
  }
  const openJob = async (id: number) => {
    try { setCandidate(undefined); setWorkflow(undefined); setJob((await api.job(id)).job); setTab('jobs'); location.hash = `job=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const archiveWorkflow = async (id: string) => {
    try { await api.workflowAction(id, 'archive'); setRefreshKey(value => value + 1) }
    catch (e) { setError(humanizeActionError(e, '归档失败，请重试。')) }
  }
  const openWorkflow = async (id: string) => {
    try { setJob(undefined); setCandidate(undefined); setWorkflow(await api.workflow(id)); location.hash = `workflow=${id}` } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const closeOverlay = () => { setJob(undefined); setCandidate(undefined); setWorkflow(undefined); history.replaceState(null, '', location.pathname) }
  const visibleJobs = jobs.filter(j => !query || `${j.id} ${j.client} ${j.title} ${j.status}`.toLowerCase().includes(query.toLowerCase()))
  const visibleCandidates = candidates.filter(c => !query || `${c.name} ${c.current_company} ${c.current_title} ${c.job} ${c.client}`.toLowerCase().includes(query.toLowerCase()))
  const copilotContext = candidate ? { type: 'candidate', id: candidate.id, candidate: candidate.name, client: candidate.client, job: candidate.job } : job ? { type: 'job', id: job.id, client: job.client, job: job.title } : workflow ? { type: 'workflow', id: workflow.workflow.workflow_id } : { type: 'page', page: tab }
  const copilotContextSignature = JSON.stringify(copilotContext)
  useEffect(() => {
    if (copilotContext.type === 'page') return
    void publishCopilotContext(copilotContext, 'selection', true)
  }, [copilotContextSignature])

  if (error && !boot) return <Diagnostics error={error} retry={() => setRefreshKey(x => x + 1)} />

  return <div className="shell">
    <aside className="nav">
      <div className="brand"><span>ASA</span><strong>Agent</strong><small>Recruiting OS</small></div>
      <nav>{tabs.map(([id, label, icon]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => { setTab(id); setQuery('') }}>{icon}<span>{label}</span></button>)}</nav>
      <a className="legacy" href="/admin/legacy" target="_blank">审计与旧版</a>
    </aside>
    <main className="main">
      <header className="topbar">
        <div><h1>{tabs.find(x => x[0] === tab)?.[1]}</h1><p>{boot?.core?.status === 'connected' ? 'ASA Agent 在线 · Core 已连接 · v3 实时数据' : 'ASA Agent 正在连接 Core'}</p></div>
        {(tab === 'jobs' || tab === 'candidates' || tab === 'progress') && <label className="search"><Search/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索姓名、公司或岗位" aria-label="搜索姓名、公司或岗位" /></label>}
        <button className="button copilot-launch" title="打开 ASA Copilot 浮窗" aria-label="打开 ASA Copilot 浮窗" onClick={()=>openCopilotWindow(copilotContext)}><MessageSquareText/><span>Copilot</span></button>
      </header>
      <div className="content">
        {tab === 'overview' && <Overview dashboard={dashboard} jobs={jobs} candidates={candidates} openWorkflow={openWorkflow} openCandidate={openCandidate} archiveWorkflow={archiveWorkflow} />}
        {tab === 'jobs' && <Jobs items={visibleJobs} onSelect={openJob} />}
        {tab === 'progress' && <Progress items={visibleCandidates} openCandidate={openCandidate} />}
        {tab === 'candidates' && <Candidates items={visibleCandidates} openCandidate={openCandidate} />}
        {tab === 'radar' && <RadarPage jobs={jobs} />}
      </div>
    </main>
    {job && <JobPanel value={job} close={closeOverlay} openCandidate={openCandidate} />}
    {candidate && <CandidatePanel value={candidate} close={closeOverlay} changed={() => refreshCandidateDetail(candidate.id)} />}
    {workflow && <WorkflowPanel value={workflow} jobs={jobs} close={closeOverlay} reload={() => openWorkflow(workflow.workflow.workflow_id)} openCandidate={openCandidate} archived={() => { closeOverlay(); setRefreshKey(value => value + 1) }} />}
    {error && <div className="toast"><ShieldAlert/> {error}<button onClick={() => setError('')}><X/></button></div>}
  </div>
}
