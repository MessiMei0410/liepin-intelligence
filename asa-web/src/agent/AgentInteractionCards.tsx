import { useState } from 'react'
import { Check, ChevronRight, CircleAlert, LoaderCircle, RotateCcw, Search, X } from 'lucide-react'
import { api } from '../api'
import { FilterNoteBatchConfirmCard } from './FilterNoteBatchConfirmCard'
import type { AgentMessage } from './sessionModel'
import type { AgentReference } from './transport'
import { recordDshConfirmation } from './transport'

const text = (value: unknown, fallback = '') => String(value ?? fallback).trim()
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' ? value as Record<string, unknown> : {}
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : []

const actionLabels: Record<string, string> = {
  open_candidate: '打开人选', open_job: '打开岗位', open_workflow: '打开工作流',
  open_analysis: '查看分析', start_workflow: '开始执行', floating_action: '执行页面动作',
  confirm_candidate_intent: '确认执行', cancel_candidate_intent: '取消',
  view_a_candidates: '查看 A 级人选', compare_top_candidates: '比较前 5 人',
  continue_sourcing: '按当前条件继续搜', relax_search: '采用 ASA 放宽方案',
  generate_contact_queue: '生成触达队列', generate_contact_script: '生成触达话术',
  confirm_advance: '确认推进', confirm_stop: '确认停止', end_round: '结束本轮',
}

export function UnderstandingCard({ card, onSelectCandidate, onReenter }: {
  card?: Record<string, unknown> | null
  onSelectCandidate?: (option: Record<string, unknown>) => void
  onReenter?: () => void
}) {
  if (!card || card.show === false) return null
  const target = record(card.target)
  const options = list(card.candidate_options).filter(item => item && typeof item === 'object').slice(0, 3) as Array<Record<string, unknown>>
  const inherited = list(card.inherited_constraints).map(item => text(item)).filter(Boolean)
  const changes = list(card.constraint_changes).map(item => typeof item === 'object' ? record(item) : { value: item }).filter(item => text(item.value || item.quote || item.label))
  const missing = list(card.missing_fields).map(item => text(item)).filter(Boolean)
  const ambiguous = options.length > 0 || Boolean(card.needs_clarification) || text(card.clarification_question)
  const actionable = ambiguous || Boolean(card.blocked_reason) || missing.length > 0 || card.show_details === true
  if (!actionable) return null
  return <section className={`agent-understanding-card ${ambiguous ? 'is-ambiguous' : ''}`} aria-label="ASA 理解卡">
    <header><span className="agent-card-mark"><Search size={14}/></span><div><b>我理解为</b><small>{text(card.action_label, '查询/说明')}</small></div><span className="agent-understanding-confidence">置信度 {Math.round(Number(card.confidence || 0) * 100)}%</span></header>
    <dl>
      <div><dt>客户</dt><dd>{text(target.client || card.client, '当前客户待确认')}</dd></div>
      <div><dt>岗位</dt><dd>{text(target.job || card.job, '当前岗位待确认')}</dd></div>
      <div><dt>人选</dt><dd>{text(target.candidate || card.candidate, '当前人选待确认')}</dd></div>
      <div><dt>目标</dt><dd>{text(card.objective, text(target.label, '当前上下文'))}</dd></div>
      <div><dt>本轮动作</dt><dd>{text(card.action_label, actionLabels[text(card.action, 'none')] || '查询/说明')}</dd></div>
      {inherited.length > 0 && <div><dt>已继承条件</dt><dd>{inherited.join('；')}</dd></div>}
      {changes.length > 0 && <div><dt>条件变化</dt><dd>{changes.map(item => text(item.value || item.quote || item.label)).join('；')}</dd></div>}
      {missing.length > 0 && <div><dt>待补充</dt><dd>{missing.join('、')}</dd></div>}
    </dl>
    {text(card.key_judgment || card.key_decision || card.reasoning) && <p className="agent-understanding-judgment"><b>ASA 判断</b>{text(card.key_judgment || card.key_decision || card.reasoning)}</p>}
    {text(card.clarification_question) && !options.length && <p className="agent-understanding-risk"><CircleAlert size={14}/>{text(card.clarification_question)}</p>}
    {text(card.blocked_reason) && <p className="agent-understanding-risk"><CircleAlert size={14}/>{text(card.blocked_reason)}</p>}
    {options.length > 0 && <div className="agent-ambiguity-options"><b>请选择唯一对象后继续</b>{options.map(option => <button key={`${text(option.type)}:${text(option.id)}`} type="button" onClick={() => onSelectCandidate?.(option)}><span><strong>{text(option.client, '未命名客户')}</strong><small>{text(option.label, '未命名对象')}</small></span><em>{text(option.status, '状态待确认')} {text(option.updated_at) && ` · ${text(option.updated_at)}`}</em><ChevronRight size={15}/></button>)}<button type="button" className="agent-ambiguity-reenter" onClick={onReenter}><RotateCcw size={14}/>重新输入名称</button></div>}
    {text(card.next_step) && !options.length && <p className="agent-understanding-next"><ChevronRight size={14}/>{text(card.next_step)}</p>}
  </section>
}

export function SuggestedActionBar({ actions, busy, onAction }: { actions?: Array<Record<string, unknown>>; busy?: string; onAction: (action: Record<string, unknown>) => void }) {
  const safe = (actions || []).filter(action => {
    const type = text(action.type)
    return ['open_candidate', 'open_job', 'open_workflow', 'open_analysis', 'start_workflow', 'floating_action', 'confirm_candidate_intent', 'cancel_candidate_intent', 'view_a_candidates', 'compare_top_candidates', 'continue_sourcing', 'relax_search', 'generate_contact_queue', 'generate_contact_script', 'confirm_advance', 'confirm_stop', 'end_round'].includes(type)
  }).slice(0, 6)
  if (!safe.length) return null
  return <div className="agent-suggested-actions" role="group" aria-label="建议动作">{safe.map((action, index) => { const type = text(action.type); const actionId = text(action.action_id, `${type}-${index}`); return <button key={actionId} type="button" disabled={Boolean(busy)} className={action.confirmation_required ? 'needs-confirmation' : ''} onClick={() => onAction(action)}>{busy === actionId ? <LoaderCircle className="spin" size={13}/> : action.confirmation_required ? <CircleAlert size={13}/> : <Check size={13}/>}<span>{text(action.label, actionLabels[type] || '执行动作')}</span></button> })}</div>
}

export function ExecutionReceipt({ receipt }: { receipt?: Record<string, unknown> | null }) {
  if (!receipt) return null
  const state = text(receipt.state, '状态待同步')
  const verified = receipt.verified === true
  const scope = record(receipt.scope)
  const counts: Array<{ label: string; value: unknown }> = [
    { label: '成功', value: receipt.succeeded }, { label: '跳过', value: receipt.skipped }, { label: '失败', value: receipt.failed },
  ].filter(item => item.value !== undefined)
  const reasons = list(receipt.reasons).map(item => text(item)).filter(Boolean)
  const visibleUnverified = Boolean(text(receipt.failure_reason)) || reasons.length > 0 || /等待|失败|阻塞|部分/.test(state)
  if (!verified && !visibleUnverified) return null
  return <section className={`agent-execution-receipt ${verified ? 'verified' : ''}`} aria-label="执行回执"><div><b>执行回执</b><strong>{state}</strong></div><p>{text(receipt.summary, '尚未执行写入或外部动作')}</p>{counts.length > 0 && <div className="agent-receipt-counts">{counts.map(item => <span key={item.label}>{item.label} <b>{String(item.value)}</b></span>)}</div>}<small>{verified ? '已完成服务端回查' : '等待真实执行结果'}{text(scope.label || scope.type) && ` · 范围 ${text(scope.label || scope.type)}`}{text(receipt.failure_reason) && ` · ${text(receipt.failure_reason)}`}{reasons.length > 0 && ` · ${reasons.join('；')}`}</small>{text(receipt.next_step) && <p className="agent-understanding-next"><ChevronRight size={14}/>{text(receipt.next_step)}</p>}</section>
}

const analysisList = (value: unknown): Array<string | Record<string, unknown>> => list(value)
  .map(item => typeof item === 'string' ? item.trim() : record(item))
  .filter(item => typeof item === 'string' ? Boolean(item) : Object.keys(item).length > 0)

const analysisItemText = (value: string | Record<string, unknown>): string => typeof value === 'string'
  ? value
  : text(value.text || value.label || value.name || value.value || value.detail || value.reason)

const analysisMetrics = (value: unknown): Array<{ label: string; value: string }> => {
  if (Array.isArray(value)) return value.map(item => {
    const metric = record(item)
    return { label: text(metric.label || metric.name || metric.key, '指标'), value: text(metric.value ?? metric.count ?? metric.amount ?? metric.rate) }
  }).filter(item => item.value)
  return Object.entries(record(value)).map(([label, metric]) => ({ label, value: text(metric) })).filter(item => item.value)
}

export function AnalysisCard({ card, onOpenAnalysis }: {
  card?: Record<string, unknown> | null
  onOpenAnalysis?: (id: string) => void
}) {
  if (!card) return null
  const data = { ...record(card.result), ...card }
  const headline = text(data.headline || data.title || data.conclusion || data.key_conclusion || data.summary)
  const metrics = analysisMetrics(data.metrics)
  const evidence = analysisList(data.evidence || data.evidences)
  const risks = analysisList(data.risk || data.risks)
  const gaps = analysisList(data.gap || data.gaps)
  const pending = analysisList(data.pending_verification || data.pending_items || data.to_verify || data['待核验'])
  const nextStep = text(data.next_step || data.nextStep || data.next_action)
  const openAction = data.open_analysis
  const openId = text(data.run_id || (typeof openAction === 'string' ? openAction : record(openAction).id || record(openAction).run_id))
  const hasContent = Boolean(headline || metrics.length || evidence.length || risks.length || gaps.length || pending.length || nextStep)
  if (!hasContent) return null
  const renderList = (label: string, items: Array<string | Record<string, unknown>>) => items.length > 0 && <div className="agent-analysis-card-list"><b>{label}</b><ul>{items.map((item, index) => <li key={`${label}-${index}`}>{analysisItemText(item)}</li>)}</ul></div>
  return <section className="agent-analysis-card" aria-label="分析卡">
    <header><span className="agent-card-mark"><Search size={14}/></span><div><b>{headline || '分析结论'}</b><small>基于当前真实证据</small></div>{openId && <button type="button" className="icon-btn" aria-label="查看分析" title="查看分析" onClick={() => onOpenAnalysis?.(openId)}><ChevronRight size={15}/></button>}</header>
    {metrics.length > 0 && <div className="agent-analysis-metrics">{metrics.map((metric, index) => <div key={`${metric.label}-${index}`}><span>{metric.label}</span><b>{` ${metric.value}`}</b></div>)}</div>}
    {renderList('证据', evidence)}
    {renderList('风险', risks)}
    {renderList('缺口', gaps)}
    {renderList('待核验', pending)}
    {nextStep && <p className="agent-understanding-next"><ChevronRight size={14}/><span><b>下一步</b>{nextStep}</span></p>}
  </section>
}

export function CandidateIntentConfirmation({ intent, sessionId }: { intent?: Record<string, unknown> | null; sessionId: string }) {
  const [cancelled, setCancelled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<Record<string, unknown>>()
  if (!intent || cancelled) return null
  const candidate = record(intent.candidate)
  const candidateId = Number(candidate.id)
  const action = text(intent.action)
  const commitCandidateIntent = async () => {
    if (!Number.isFinite(candidateId) || !action || busy) return
    setBusy(true); setError('')
    try {
      const response = await api.confirmCopilotIntent({
        intent: { kind: text(intent.kind, 'candidate_action') as 'candidate_action', action, message: text(intent.message) },
        intent_hash: text(intent.intent_hash), candidate_id: candidateId,
        preflight_token: text(intent.preflight_token), message: text(intent.message), session_id: sessionId,
      })
      setResult(response)
    } catch (value) { setError(value instanceof Error ? value.message : String(value)) }
    finally { setBusy(false) }
  }
  if (result) return <section className="agent-execution-receipt verified" aria-label="候选人执行回执"><div><b>执行回执</b><strong>已完成</strong></div><p>{text(result.answer, `${text(candidate.name, '当前人选')}状态已更新`)}</p><small>已完成服务端回查</small></section>
  return <section className="agent-pending-intent" aria-label="候选人动作预检"><header><div><small>写入预检已通过</small><b>{text(intent.action_label, actionLabels[action] || '候选人状态动作')}</b></div><button type="button" aria-label="取消本次候选人动作" onClick={() => setCancelled(true)}><X size={15}/></button></header><dl><div><dt>人选</dt><dd>{text(candidate.name, `#${candidateId}`)}</dd></div><div><dt>岗位</dt><dd>{[text(candidate.client), text(candidate.job)].filter(Boolean).join(' / ') || '待确认'}</dd></div><div><dt>当前阶段</dt><dd>{text(candidate.stage, '待确认')}</dd></div></dl><p>{text(intent.confirm_text, '确认后将写入候选人状态。')}</p>{error && <div className="agent-card-error" role="alert">{error}</div>}<footer><button type="button" onClick={() => setCancelled(true)}>取消</button><button type="button" className="primary" disabled={busy} onClick={() => void commitCandidateIntent()}>{busy ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}确认执行</button></footer></section>
}

// ── DSH 写确认卡（人确认闸门的 UI 侧）────────────────────────────────────
// 模型（DSH 脑）对写动作只能发起 preflight 申请；asa-server 把 confirm_request
// 透传到前端渲染本卡。用户点确认 → 前端调 Core activate（UA 门控，模型拿不到）
// + 写端点（带 Idempotency-Key）；取消则零写请求。四态语义同浮窗确认卡：
// pending（待确认）/ confirmed（已确认）/ cancelled（已取消）/ drift（409 漂移），
// 另加 expired（5 分钟 token 过期，按 expires_at 本地判定，不发任何请求）。
// 终态经 record-turn confirm_result 回写 Core，会话恢复后呈现终态。
// expired / drift（409）不是死路：卡片用自身携带的参数重新走对应 preflight
// 端点换新 token，回到 pending 待确认——确认动作仍由人点，重预检不直接执行。

const candidateActionText: Record<string, string> = {
  advance: '复核通过', contact: '已联系', recommend: '已推荐给客户', stop: '停止推进', review: '评分复核', merge: '合并去重',
  record_event: '记录面试/事件',
}
const decisionText: Record<string, string> = { approve: '批准', reject: '拒绝', revise: '退回修改' }
const workflowActionText: Record<string, string> = { cancel: '关闭工作流', pause: '暂停工作流', resume: '恢复工作流' }
// 简历回填 diff 变化类型（Core service_resume_backfill 口径）：
// added=本地为空将新增 / updated=两端都有且不同将更新 / unchanged=一致 / kept=本地已有值保留（people 只回填空字段）。
const resumeDiffChangeText: Record<string, string> = { added: '新增', updated: '更新', unchanged: '无变化', kept: '保留原值' }

export function WriteConfirmationCard({ request, sessionId }: { request?: Record<string, unknown> | null; sessionId: string }) {
  const [cancelled, setCancelled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<{ summary: string; receipt?: Record<string, unknown> }>()
  // 重预检签发的新 token/过期时间（expired 与 409 漂移共用）；确认仍用最新一份。
  const [refreshed, setRefreshed] = useState<{ token: string; expiresAt: string } | null>(null)
  const [repreflightBusy, setRepreflightBusy] = useState(false)
  if (!request) return null
  const kind = text(request.kind)
  // 批量口径便签（filter_note_batch，多岗位一张卡）：独立组件承载，状态机与本卡一致。
  if (kind === 'filter_note_batch') return <FilterNoteBatchConfirmCard request={request} sessionId={sessionId} />
  const persistedState = text(request.state, 'pending')
  const candidate = record(request.candidate)
  const approval = record(request.approval)
  const workflow = record(request.workflow)
  // 合并去重（action=merge）：candidate=保留方（winner），merge 带废弃方与字段 diff。
  const merge = record(request.merge)
  const mergeLoser = record(merge.loser)
  const isMerge = kind === 'candidate_action' && text(request.action) === 'merge'
  const mergeDiff = (list(merge.diff).filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>)
  // 生命周期事件（action=record_event）：event 带事件类型/时间/状态/备注（Core 预检回显）。
  const isRecordEvent = kind === 'candidate_action' && text(request.action) === 'record_event'
  const eventInfo = record(request.event)
  // 简历回填：resume 带快照元信息，diff 为分段新旧比对（字数 + 摘要 + 变化类型）。
  const isResumeBackfill = kind === 'resume_backfill'
  const backfillResume = record(request.resume)
  const backfillDiff = (list(request.diff).filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>)
  // 岗位筛选口径便签（R2-3）：job 带岗位信息，note=新便签，previous_note=当前便签对照。
  const isFilterNote = kind === 'filter_note'
  const filterNoteJob = record(request.job)
  // 岗位建档：job 带客户解析结果（client_is_new/client_match）与岗位字段全量回显，
  // warnings 带模糊匹配/缺 JD 等提示（重复岗位 Core 预检已 409，到不了确认卡）。
  const isJobCreate = kind === 'job_create'
  const jobCreate = record(request.job)
  const jobCreateWarnings = list(request.warnings).map(item => text(item)).filter(Boolean)
  const activeToken = refreshed?.token || text(request.preflight_token)
  const expiresAt = Date.parse(refreshed?.expiresAt || text(request.expires_at))
  const expired = persistedState === 'pending' && Number.isFinite(expiresAt) && expiresAt <= Date.now()
  const clientRequestId = text(request.client_request_id)

  const title = kind === 'approval_decision'
    ? `审批决定：${decisionText[text(approval.decision)] || text(approval.decision, '审批决定')}`
    : kind === 'workflow_action'
      ? workflowActionText[text(request.action)] || '工作流动作'
      : isResumeBackfill
        ? '简历回填'
        : isJobCreate
          ? '岗位建档'
          : isFilterNote
            ? '保存筛选口径便签'
            : `候选人动作：${candidateActionText[text(request.action)] || text(request.action, '状态动作')}`
  const targetLines: Array<{ label: string; value: string }> = kind === 'approval_decision'
    ? [
        { label: '审批', value: text(approval.title, text(approval.approval_id, '待审批事项')) },
        { label: '目标', value: text(approval.goal_title, text(approval.workflow_id, '待确认')) },
      ]
    : kind === 'workflow_action'
      ? [
          { label: '工作流', value: text(workflow.title, text(workflow.workflow_id, '待确认')) },
          { label: '当前状态', value: text(workflow.status, '待确认') },
        ]
      : isFilterNote
        ? [
            { label: '岗位', value: [text(filterNoteJob.client), text(filterNoteJob.title)].filter(Boolean).join(' / ') || `岗位 #${text(filterNoteJob.id)}` },
            { label: '新便签', value: text(request.note) },
            { label: '当前便签', value: text(request.previous_note, '无') },
          ]
      : isJobCreate
        ? [
            { label: '客户', value: `${text(jobCreate.client, '待确认')}${jobCreate.client_is_new === true ? '（将新建客户）' : ''}` },
            { label: '岗位', value: text(jobCreate.title, '待确认') },
            { label: '方向', value: text(jobCreate.direction, '未填写') },
            { label: 'Base', value: text(jobCreate.base, '未填写') },
            ...(text(jobCreate.priority) ? [{ label: '优先级', value: text(jobCreate.priority) }] : []),
            { label: 'JD', value: text(jobCreate.jd_text) || '未提供（建档后待补充）' },
            ...jobCreateWarnings.map(warning => ({ label: '提示', value: warning })),
          ]
      : isResumeBackfill
        ? [
            { label: '人选', value: `${text(candidate.name, `#${text(candidate.id)}`)}（${[text(candidate.client), text(candidate.job)].filter(Boolean).join(' / ') || '岗位待确认'}）` },
            { label: '当前阶段', value: text(candidate.stage, '待确认') },
            { label: '简历来源', value: `猎聘详情页（档案 ${text(backfillResume.resume_id, '未知')}）` },
            { label: '抓取时间', value: text(backfillResume.captured_at, '待确认') },
          ]
        : isMerge
        ? [
            { label: '保留方', value: `${text(candidate.name, '待确认')}（关系 #${text(candidate.id)}）` },
            { label: '废弃方', value: `${text(mergeLoser.name, '待确认')}（关系 #${text(mergeLoser.id)}）` },
            { label: '废弃方阶段', value: text(mergeLoser.stage, '待确认') },
          ]
        : isRecordEvent
        ? [
            { label: '人选', value: `${text(candidate.name, `#${text(candidate.id)}`)}（当前阶段：${text(candidate.stage, '待确认')}）` },
            { label: '事件', value: text(eventInfo.label, text(eventInfo.event_type, '待确认')) },
            { label: '事件时间', value: text(eventInfo.occurred_at, '记录当前时间') },
            { label: '事件状态', value: text(eventInfo.event_status, '默认') },
          ]
        : [
            { label: '人选', value: text(candidate.name, `#${text(candidate.id)}`) },
            { label: '当前阶段', value: text(candidate.stage, '待确认') },
          ]
  const noteText = text(request.note)

  const backfill = (state: 'confirmed' | 'cancelled', summary: string, receipt?: Record<string, unknown>) => {
    void recordDshConfirmation(sessionId, clientRequestId, { state, summary, ...(receipt ? { execution_receipt: receipt } : {}) })
  }
  const confirmWrite = async () => {
    if (busy) return
    setBusy(true); setError('')
    try {
      let summary = ''
      if (kind === 'approval_decision') {
        await api.approval(text(approval.approval_id), text(approval.decision), activeToken, noteText)
        summary = `已${decisionText[text(approval.decision)] || '处理'}审批：${text(approval.title, text(approval.approval_id))}`
      } else if (kind === 'workflow_action') {
        await api.workflowAction(text(workflow.workflow_id), text(request.action), { note: noteText }, activeToken)
        summary = `已执行：${workflowActionText[text(request.action)] || '工作流动作'}`
      } else if (isResumeBackfill) {
        const candidateId = Number(candidate.id)
        if (!Number.isFinite(candidateId)) {
          setError('确认请求缺少候选人信息')
          return
        }
        const response = await api.resumeBackfillCommit(candidateId, activeToken, noteText)
        summary = response.already_applied
          ? `${text(candidate.name, '当前人选')}的简历已是最新，无需重复回填`
          : text(response.summary, `已回填 ${text(candidate.name, '当前人选')} 的简历`)
      } else if (isFilterNote) {
        const jobId = Number(filterNoteJob.id)
        if (!Number.isFinite(jobId) || !text(request.note)) {
          setError('确认请求缺少岗位或便签内容')
          return
        }
        const response = await api.jobFilterNoteCommit(jobId, text(request.note), activeToken)
        summary = response.already_saved
          ? '该口径便签此前已保存，未重复写入'
          : `已保存筛选口径便签：${[text(filterNoteJob.client), text(filterNoteJob.title)].filter(Boolean).join(' / ') || `岗位 #${jobId}`}——出名单时将随口径声明显示`
      } else if (isJobCreate) {
        const clientName = text(jobCreate.client)
        const title = text(jobCreate.title)
        if (!clientName || !title) {
          setError('确认请求缺少客户或岗位信息')
          return
        }
        const response = await api.jobCreateCommit({
          client_name: clientName, title,
          direction: text(jobCreate.direction), base: text(jobCreate.base),
          jd_text: text(jobCreate.jd_text), priority: text(jobCreate.priority),
        }, activeToken)
        summary = response.already_created
          ? `该岗位此前已建档（岗位 #${response.job_id}），未重复创建`
          : `已建档：岗位 #${response.job_id}（${text(response.client_name, clientName)} / ${text(response.title, title)}）${response.client_created ? '，同时新建了客户档案' : ''}——初始状态待启动，不会自动启动寻访`
      } else {
        const candidateId = Number(candidate.id)
        if (!Number.isFinite(candidateId) || !text(request.action)) {
          setError('确认请求缺少候选人或动作信息')
          return
        }
        if (isMerge) {
          const loserId = Number(mergeLoser.id)
          if (!Number.isFinite(loserId)) {
            setError('确认请求缺少废弃方（loser）信息')
            return
          }
          await api.commit(candidateId, 'merge', activeToken, noteText, undefined, loserId)
          summary = `已合并去重：${text(mergeLoser.name, '废弃方')} 已停止并指向 ${text(candidate.name, '保留方')}`
        } else if (isRecordEvent) {
          const eventType = text(eventInfo.event_type)
          if (!eventType) {
            setError('确认请求缺少事件类型')
            return
          }
          const response = await api.commit(candidateId, 'record_event', activeToken, text(eventInfo.notes, noteText), undefined, undefined, {
            event_type: eventType,
            event_status: text(eventInfo.event_status) || undefined,
            occurred_at: text(eventInfo.occurred_at) || undefined,
          })
          const recorded = record(response.event)
          summary = response.already_recorded
            ? `${text(candidate.name, '当前人选')}的「${text(eventInfo.label, '事件')}」此前已记录，未重复写入`
            : `已记录：${text(candidate.name, '当前人选')}「${text(recorded.event_type_label, text(eventInfo.label, '事件'))}」${text(recorded.event_time, text(eventInfo.occurred_at))}`
        } else {
          const response = await api.commit(candidateId, text(request.action), activeToken, noteText)
          summary = `已同步到 ASA：${text(candidate.name, '当前人选')} ${candidateActionText[text(request.action)] || '状态已更新'}${response.stage ? `，当前阶段为“${response.stage}”` : ''}`
        }
      }
      const receipt = {
        version: 'execution_receipt_v1', state: '已完成', summary,
        succeeded: 1, skipped: 0, failed: 0, verified: true,
      }
      setResult({ summary, receipt })
      backfill('confirmed', summary, receipt)
    } catch (value) {
      // 409 漂移（token 过期/已用、审批已处理、状态已变化）：展示服务端中文 detail，不重试。
      setError(value instanceof Error ? value.message : String(value))
    } finally {
      setBusy(false)
    }
  }
  const cancelWrite = () => {
    if (busy) return
    setCancelled(true)
    backfill('cancelled', '用户取消，未写入')
  }
  // 重新预检：expired（token 5 分钟过期）与 drift（409 漂移失败）共用——用卡片
  // 自身携带的参数走对应 preflight 端点换新 token，成功后回到 pending 待确认。
  // 重预检只换 token，不执行写入；确认动作仍由人点「确认执行」。
  const repreflight = async () => {
    if (busy || repreflightBusy) return
    setRepreflightBusy(true); setError('')
    try {
      let token = ''
      let newExpiresAt = ''
      if (kind === 'approval_decision') {
        const approvalId = text(approval.approval_id)
        if (!approvalId) { setError('确认请求缺少审批信息'); return }
        const response = await api.approvalPreflight(approvalId, text(approval.decision), noteText)
        token = response.token; newExpiresAt = response.expires_at
      } else if (kind === 'workflow_action') {
        const workflowId = text(workflow.workflow_id)
        if (!workflowId || !text(request.action)) { setError('确认请求缺少工作流或动作信息'); return }
        const response = await api.workflowActionPreflight(workflowId, text(request.action), noteText)
        token = response.token; newExpiresAt = response.expires_at
      } else if (isResumeBackfill) {
        const candidateId = Number(candidate.id)
        if (!Number.isFinite(candidateId)) { setError('确认请求缺少候选人信息'); return }
        const response = await api.resumeBackfillPreflight(candidateId, text(backfillResume.resume_id))
        if (response.unchanged || !response.token) {
          setError(text(response.message, '页面简历与本地档案一致，无需回填。'))
          return
        }
        token = response.token; newExpiresAt = text(response.expires_at)
      } else if (isFilterNote) {
        const jobId = Number(filterNoteJob.id)
        if (!Number.isFinite(jobId) || !text(request.note)) { setError('确认请求缺少岗位或便签内容'); return }
        const response = await api.jobFilterNotePreflight(jobId, text(request.note))
        token = response.token; newExpiresAt = text(response.expires_at)
      } else if (isJobCreate) {
        const clientName = text(jobCreate.client)
        const title = text(jobCreate.title)
        if (!clientName || !title) { setError('确认请求缺少客户或岗位信息'); return }
        const response = await api.jobCreatePreflight({
          client_name: clientName, title,
          direction: text(jobCreate.direction), base: text(jobCreate.base),
          jd_text: text(jobCreate.jd_text), priority: text(jobCreate.priority),
        })
        token = response.token; newExpiresAt = text(response.expires_at)
      } else {
        const candidateId = Number(candidate.id)
        if (!Number.isFinite(candidateId) || !text(request.action)) { setError('确认请求缺少候选人或动作信息'); return }
        if (isMerge && !Number.isFinite(Number(mergeLoser.id))) { setError('确认请求缺少废弃方（loser）信息'); return }
        if (isRecordEvent && !text(eventInfo.event_type)) { setError('确认请求缺少事件类型'); return }
        const response = await api.preflight(candidateId, text(request.action), {
          ...(isMerge ? { loser_id: Number(mergeLoser.id) } : {}),
          ...(isRecordEvent ? {
            note: text(eventInfo.notes, noteText),
            event: {
              event_type: text(eventInfo.event_type),
              event_status: text(eventInfo.event_status) || undefined,
              occurred_at: text(eventInfo.occurred_at) || undefined,
            },
          } : {}),
        })
        token = response.token; newExpiresAt = text(response.expires_at)
      }
      setRefreshed({ token, expiresAt: newExpiresAt })
    } catch (value) {
      let message = value instanceof Error ? value.message : String(value)
      // 简历快照 30 分钟 TTL：Core 409「未读到当前页简历快照…」映射为可操作的明确提示。
      if (isResumeBackfill && /未读到当前页简历快照/.test(message)) {
        message = '页面快照已过期，请在详情页重新打开后再试'
      }
      setError(message)
    } finally {
      setRepreflightBusy(false)
    }
  }
  const repreflightButton = <button type="button" disabled={busy || repreflightBusy} onClick={() => void repreflight()}>{repreflightBusy ? <LoaderCircle className="spin" size={14}/> : <RotateCcw size={14}/>}重新预检</button>

  if (result) {
    return <section className="agent-execution-receipt verified" aria-label="写入执行回执"><div><b>执行回执</b><strong>已完成</strong></div><p>{result.summary}</p><small>已完成服务端写入</small></section>
  }
  if (persistedState === 'confirmed') {
    return <section className="agent-execution-receipt verified" aria-label="写入执行回执"><div><b>执行回执</b><strong>已完成</strong></div><p>{text(request.result_summary, '该写入已确认并同步到 ASA')}</p><small>已完成服务端写入</small></section>
  }
  if (persistedState === 'cancelled' || cancelled) {
    return <section className="agent-pending-intent is-closed" aria-label="写入确认已取消"><header><div><small>写入确认</small><b>{title}</b></div></header><p>已取消，未写入 ASA。</p></section>
  }
  if (expired) {
    return <section className="agent-pending-intent is-closed" aria-label="写入确认已过期"><header><div><small>写入确认</small><b>{title}</b></div></header><p>确认请求已过期（5 分钟有效），未写入 ASA；可直接重新预检后继续，无需重新下指令。</p>{error && <div className="agent-card-error" role="alert">{error}</div>}<footer><button type="button" onClick={cancelWrite} disabled={busy || repreflightBusy}>取消</button>{repreflightButton}</footer></section>
  }
  return <section className="agent-pending-intent" aria-label="写入确认">
    <header><div><small>ASA 发起写入申请</small><b>{title}</b></div><button type="button" aria-label="取消本次写入" onClick={cancelWrite} disabled={busy}><X size={15}/></button></header>
    <dl>{targetLines.map((line, index) => <div key={`${line.label}-${index}`}><dt>{line.label}</dt><dd>{line.value}</dd></div>)}{noteText && <div><dt>原因</dt><dd>{noteText}</dd></div>}</dl>
    {isMerge && mergeDiff.length > 0 && <dl aria-label="合并字段比对">{mergeDiff.map(item => <div key={text(item.field)}><dt>{text(item.label, '字段')}</dt><dd>{item.same === true ? text(item.winner, '—') : `保留：${text(item.winner, '—')} ｜ 废弃：${text(item.loser, '—')}`}</dd></div>)}</dl>}
    {isResumeBackfill && backfillDiff.length > 0 && <dl aria-label="简历新旧比对">{backfillDiff.map(item => {
      const change = text(item.change, 'unchanged')
      return <div key={text(item.field)} data-change={change}>
        <dt>{text(item.label, '字段')}</dt>
        <dd>{change === 'unchanged'
          ? `无变化（${Number(item.before_chars) || 0} 字）`
          : change === 'kept'
            ? `保留原值：${text(item.before_excerpt, '—')}`
            : `${resumeDiffChangeText[change] || change}：本地 ${Number(item.before_chars) || 0} 字 → 页面 ${Number(item.after_chars) || 0} 字`}
          {(change === 'added' || change === 'updated') && text(item.after_excerpt) && <small>{text(item.after_excerpt)}</small>}
        </dd>
      </div>
    })}</dl>}
    <p>{text(request.impact, '确认后将写入 ASA，并记入统一审计。')}</p>
    {error && <div className="agent-card-error" role="alert">{error}</div>}
    <footer><button type="button" onClick={cancelWrite} disabled={busy || repreflightBusy}>取消</button>{error && repreflightButton}<button type="button" className="primary" disabled={busy || repreflightBusy || Boolean(error)} onClick={() => void confirmWrite()}>{busy ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}确认执行</button></footer>
  </section>
}

export type InteractionMessage = Pick<AgentMessage, 'understanding_card' | 'execution_receipt' | 'suggested_actions'>
export type InteractionReference = AgentReference
