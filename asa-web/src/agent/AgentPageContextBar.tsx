import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ChevronDown, ClipboardCheck, Database, ExternalLink, FileSearch, LoaderCircle, RefreshCw, UserRoundSearch } from 'lucide-react'
import { api, FloatingBridgeContext, FloatingStatePayload } from '../api'
import type { AgentReference } from './transport'

const POLL_INTERVAL_MS = 12_000

const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object'
  ? value as Record<string, unknown>
  : {}

const readableError = (value: unknown) => value instanceof Error ? value.message : String(value)

const expectedContextPayload = (raw: Record<string, unknown>, jobCandidateId?: number) => ({
  expected_context_key: String(raw.context_key || ''),
  expected_instance_id: String(raw.instance_id || ''),
  expected_job_candidate_id: jobCandidateId,
  expected_context_revision: String(raw.context_revision || raw.identity_fingerprint || ''),
})

const floatingContextIdentity = (payload?: FloatingStatePayload) => {
  const active = payload?.active_context || {}
  const raw = asRecord(payload?.active_context_raw)
  return [
    active.surface || raw.surface,
    raw.context_key,
    raw.instance_id,
    active.job_candidate_id || raw.job_candidate_id,
    active.type,
    active.id,
    active.title,
    active.subtitle,
    raw.res_id_encode,
    raw.resume_id,
    raw.candidate_id,
    raw.profile_id,
    raw.url,
    raw.context_revision || raw.identity_fingerprint,
  ].map(value => String(value || '').trim()).join('|')
}

const contextAgeLabel = (seconds: unknown, updatedAt?: string) => {
  const age = Number(seconds)
  if (Number.isFinite(age) && age >= 0) {
    if (age < 10) return '刚刚同步'
    if (age < 60) return `${Math.round(age)} 秒前同步`
    if (age < 3600) return `${Math.round(age / 60)} 分钟前同步`
    return `${Math.round(age / 3600)} 小时前同步`
  }
  if (!updatedAt) return '同步时间未知'
  return `同步于 ${updatedAt.replace('T', ' ').slice(0, 16)}`
}

const actionMessage = (action: string, result: Record<string, unknown>) => {
  if (typeof result.message === 'string' && result.message.trim()) return result.message
  if (action === 'assess_current') {
    return result.status === 'completed' ? '简历评估已完成，可打开人选查看完整结果。' : '简历评估已启动，可稍后打开人选查看结果。'
  }
  if (action === 'generate_report') return '推荐报告生成计划已建立；启动后会生成匹配分析和嘉驰推荐报告。'
  return '操作已提交。'
}

const TERMINAL_ASSESSMENT_STATUSES = new Set(['completed', 'failed', 'interrupted', 'stale'])
const wait = (milliseconds: number) => new Promise(resolve => window.setTimeout(resolve, milliseconds))

export function AgentPageContextBar({ onOpenFullObject, onBridgeContextChange }: {
  onOpenFullObject: (reference: AgentReference) => void
  onBridgeContextChange?: (context?: FloatingBridgeContext) => void
}) {
  const [state, setState] = useState<FloatingStatePayload>()
  const [loadError, setLoadError] = useState('')
  const [busyAction, setBusyAction] = useState('')
  const [feedback, setFeedback] = useState<{
    kind: 'success' | 'error' | 'notice'; message: string; action?: string; workflowId?: string;
    targetCandidateId?: number; targetTitle?: string; targetSubtitle?: string;
  }>()
  const [expanded, setExpanded] = useState(false)
  const refreshSeqRef = useRef(0)
  const actionSeqRef = useRef(0)
  const activeContextIdentityRef = useRef('')

  const refreshState = useCallback(async (quiet = false) => {
    const seq = ++refreshSeqRef.current
    try {
      const result = await api.floatingState()
      if (seq !== refreshSeqRef.current) return
      const nextIdentity = floatingContextIdentity(result)
      if (activeContextIdentityRef.current && nextIdentity !== activeContextIdentityRef.current) {
        actionSeqRef.current += 1
        setBusyAction('')
        setFeedback(undefined)
      }
      activeContextIdentityRef.current = nextIdentity
      setState(result)
      setLoadError('')
    } catch (value) {
      if (seq === refreshSeqRef.current && !quiet) setLoadError(readableError(value))
    }
  }, [])

  useEffect(() => {
    void Promise.resolve().then(() => refreshState())
    const pollTimer = window.setInterval(() => void refreshState(), POLL_INTERVAL_MS)
    return () => {
      window.clearInterval(pollTimer)
      refreshSeqRef.current += 1
      actionSeqRef.current += 1
    }
  }, [refreshState])

  const active = state?.active_context || {}
  const raw = asRecord(state?.active_context_raw)
  const quality = asRecord(state?.context_quality)
  const surface = String(active.surface || raw.surface || '')
  const hasContext = Boolean(Object.keys(raw).length || (surface && surface !== 'global' && surface !== 'unknown'))
  const jobCandidateId = Number(active.job_candidate_id || raw.job_candidate_id || 0) || undefined
  const stale = Boolean(active.stale ?? quality.stale ?? raw.stale)
  const bridgeContext = useMemo<FloatingBridgeContext | undefined>(() => {
    if (!hasContext) return undefined
    return {
      surface,
      context_key: String(raw.context_key || ''),
      instance_id: String(raw.instance_id || ''),
      job_candidate_id: jobCandidateId,
      title: active.title,
      subtitle: active.subtitle,
      updated_at: active.updated_at || String(raw.updated_at || ''),
      context_revision: String(raw.context_revision || raw.identity_fingerprint || ''),
      identity_fingerprint: String(raw.identity_fingerprint || raw.context_revision || ''),
      stale,
    }
  }, [active.subtitle, active.title, active.updated_at, hasContext, jobCandidateId, raw.context_key, raw.context_revision, raw.identity_fingerprint, raw.instance_id, raw.updated_at, stale, surface])

  useEffect(() => onBridgeContextChange?.(bridgeContext), [bridgeContext, onBridgeContextChange])

  const runFloatingAction = async (action: string) => {
    if (busyAction) return
    const actionSeq = ++actionSeqRef.current
    const actionIdentity = activeContextIdentityRef.current
    const targetCandidateId = jobCandidateId
    const targetTitle = active.title
    const targetSubtitle = active.subtitle
    const actionIsCurrent = () => actionSeq === actionSeqRef.current && actionIdentity === activeContextIdentityRef.current
    setBusyAction(action); setFeedback(undefined)
    try {
      const result = await api.floatingAction(action, expectedContextPayload(raw, targetCandidateId))
      if (!actionIsCurrent()) return
      const record = result as Record<string, unknown>
      setFeedback({
        kind: result.ok === false ? 'error' : 'success', message: actionMessage(action, record), action,
        workflowId: result.workflow_id, targetCandidateId, targetTitle, targetSubtitle,
      })
      void refreshState(true)
    } catch (value) {
      if (actionIsCurrent()) {
        const error = value as Error & { status?: number; errorCode?: string }
        const message = error.status === 409 && error.errorCode === 'context_changed'
          ? '当前页面已变化，动作未执行。请刷新页面识别后重新确认。'
          : readableError(value)
        setFeedback({ kind: 'error', message, targetCandidateId, targetTitle, targetSubtitle })
      }
    } finally {
      if (actionIsCurrent()) setBusyAction('')
    }
  }

  const assessCandidate = async () => {
    if (!jobCandidateId) {
      setFeedback({
        kind: 'notice',
        message: surface === 'liepin' ? '请先补全简历并定位，再进行简历评估。' : '请先完成人选入库预检并定位唯一人岗关系。',
      })
      return
    }
    if (busyAction) return
    const actionSeq = ++actionSeqRef.current
    const actionIdentity = activeContextIdentityRef.current
    const targetCandidateId = jobCandidateId
    const targetTitle = active.title
    const targetSubtitle = active.subtitle
    const actionIsCurrent = () => actionSeq === actionSeqRef.current && actionIdentity === activeContextIdentityRef.current
    setBusyAction('assess_current'); setFeedback(undefined)
    try {
      let result = await api.agentCandidateAssess(targetCandidateId, true)
      if (!actionIsCurrent()) return
      const runId = String(result.run_id || '')
      if (runId && !TERMINAL_ASSESSMENT_STATUSES.has(String(result.status || ''))) {
        setFeedback({
          kind: 'notice', message: result.coalesced ? '该人选已有评估正在运行，正在等待同一任务完成。' : '简历评估正在运行，完成后会自动更新回执。',
          targetCandidateId, targetTitle, targetSubtitle,
        })
        for (let attempt = 0; attempt < 60; attempt += 1) {
          result = await api.agentRun(runId)
          if (!actionIsCurrent()) return
          if (TERMINAL_ASSESSMENT_STATUSES.has(String(result.status || ''))) break
          await wait(1_000)
          if (!actionIsCurrent()) return
        }
      }
      const status = String(result.status || '')
      if (status === 'completed') {
        setFeedback({
          kind: 'success', message: result.cached ? '简历与岗位依据未变化，已返回现有评估。' : '简历评估已完成，可打开人选查看完整结果。',
          targetCandidateId, targetTitle, targetSubtitle,
        })
      } else if (['failed', 'interrupted', 'stale'].includes(status)) {
        setFeedback({
          kind: 'error', message: String(result.error || '简历评估未完成，请打开人选核对依据后重试。'),
          targetCandidateId, targetTitle, targetSubtitle,
        })
      } else {
        setFeedback({ kind: 'notice', message: '简历评估仍在后台运行，可打开人选查看最新状态。', targetCandidateId, targetTitle, targetSubtitle })
      }
    } catch (value) {
      if (actionIsCurrent()) setFeedback({ kind: 'error', message: readableError(value), targetCandidateId, targetTitle, targetSubtitle })
    } finally {
      if (actionIsCurrent()) setBusyAction('')
    }
  }

  const openCandidate = () => {
    if (!jobCandidateId) return
    onOpenFullObject({
      type: 'candidate',
      id: jobCandidateId,
      label: active.title || `人选 #${jobCandidateId}`,
      subtitle: active.subtitle,
    })
  }

  const openWorkflow = () => {
    if (!feedback?.workflowId) return
    onOpenFullObject({ type: 'workflow', id: feedback.workflowId, label: '推荐报告生成计划', subtitle: feedback.targetTitle })
  }

  const openFeedbackCandidate = () => {
    if (!feedback?.targetCandidateId) return
    onOpenFullObject({
      type: 'candidate', id: feedback.targetCandidateId,
      label: feedback.targetTitle || `人选 #${feedback.targetCandidateId}`,
      subtitle: feedback.targetSubtitle,
    })
  }

  const className = `agent-page-context ${stale ? 'stale' : ''} ${loadError ? 'unavailable' : ''} ${expanded ? 'expanded' : 'collapsed'}`
  if (!expanded) {
    return <section className={className} aria-label="当前页面">
      <button className="agent-page-context-toggle" type="button" aria-label="显示当前页面识别" title={hasContext ? String(active.title || '当前页面') : '当前页面识别'} onClick={() => setExpanded(true)}>
        <UserRoundSearch/>
      </button>
    </section>
  }

  return <section className={className} aria-label="当前页面">
    <div className="agent-page-context-main">
      <span className="agent-page-context-icon"><UserRoundSearch/></span>
      <span className="agent-page-context-copy">
        <small>当前页面 · {hasContext ? (active.source_label || '已识别') : '未识别'}</small>
        <b>{hasContext ? (active.title || '当前页面') : '尚未识别候选人页面'}</b>
        <em>{hasContext
          ? [active.subtitle, jobCandidateId ? `人岗关系 #${jobCandidateId}` : '未定位唯一人岗关系', stale ? '已过期' : contextAgeLabel(active.age_seconds ?? quality.age_seconds, active.updated_at)].filter(Boolean).join(' · ')
          : (loadError ? '页面识别服务暂不可用' : '切换到猎聘、X-SaaS 或 A 系统后会自动识别')}</em>
      </span>
      <button className="icon-btn" type="button" title="刷新识别" aria-label="刷新识别" disabled={Boolean(busyAction)} onClick={() => void runFloatingAction('refresh_bridge')}>
        {busyAction === 'refresh_bridge' ? <LoaderCircle className="spin"/> : <RefreshCw/>}
      </button>
      <button className="icon-btn" type="button" title="收起页面识别" aria-label="收起当前页面识别" onClick={() => setExpanded(false)}>
        <ChevronDown/>
      </button>
    </div>
    {hasContext && <div className="agent-page-context-actions">
      {jobCandidateId && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={openCandidate}>
        <ExternalLink/>打开人选
      </button>}
      {jobCandidateId && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void assessCandidate()}>
        {busyAction === 'assess_current' ? <LoaderCircle className="spin"/> : <ClipboardCheck/>}评估简历
      </button>}
      {jobCandidateId && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void runFloatingAction('generate_report')}>
        {busyAction === 'generate_report' ? <LoaderCircle className="spin"/> : <FileSearch/>}生成推荐报告
      </button>}
      {!jobCandidateId && surface === 'liepin' && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void runFloatingAction('fill_resume')}>
        {busyAction === 'fill_resume' ? <LoaderCircle className="spin"/> : <Database/>}补全简历并定位
      </button>}
      {!jobCandidateId && surface === 'liepin' && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void assessCandidate()}>
        {busyAction === 'assess_current' ? <LoaderCircle className="spin"/> : <ClipboardCheck/>}评估简历
      </button>}
      {!jobCandidateId && surface === 'xsaas' && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void runFloatingAction('dry-intake')}>
        {busyAction === 'dry-intake' ? <LoaderCircle className="spin"/> : <Database/>}入库预检
      </button>}
      {!jobCandidateId && surface === 'xsaas' && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void runFloatingAction('dry-continue')}>
        {busyAction === 'dry-continue' ? <LoaderCircle className="spin"/> : <FileSearch/>}推进预检
      </button>}
      {!jobCandidateId && surface === 'xsaas' && <button className="button" type="button" disabled={Boolean(busyAction)} onClick={() => void assessCandidate()}>
        {busyAction === 'assess_current' ? <LoaderCircle className="spin"/> : <ClipboardCheck/>}评估简历
      </button>}
    </div>}
    {(feedback || loadError) && <div className={`agent-page-context-feedback ${feedback?.kind || 'error'}`} role="status">
      {(feedback?.kind === 'error' || loadError) && <AlertTriangle/>}
      <span>{feedback?.message || loadError}</span>
      {feedback?.kind === 'success' && feedback.action === 'generate_report' && feedback.workflowId && <button type="button" onClick={openWorkflow}>打开生成计划</button>}
      {feedback?.kind === 'success' && feedback.action !== 'generate_report' && feedback.targetCandidateId && <button type="button" onClick={openFeedbackCandidate}>打开人选详情</button>}
    </div>}
  </section>
}
