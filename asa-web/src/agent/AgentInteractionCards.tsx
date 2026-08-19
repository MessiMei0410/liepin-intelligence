import { useState } from 'react'
import { Check, ChevronRight, CircleAlert, LoaderCircle, RotateCcw, Search, X } from 'lucide-react'
import { api } from '../api'
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

const candidateActionText: Record<string, string> = {
  advance: '复核通过', contact: '已联系', recommend: '已推荐给客户', stop: '停止推进', review: '评分复核', merge: '合并去重',
}
const decisionText: Record<string, string> = { approve: '批准', reject: '拒绝', revise: '退回修改' }
const workflowActionText: Record<string, string> = { cancel: '关闭工作流', pause: '暂停工作流', resume: '恢复工作流' }

export function WriteConfirmationCard({ request, sessionId }: { request?: Record<string, unknown> | null; sessionId: string }) {
  const [cancelled, setCancelled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<{ summary: string; receipt?: Record<string, unknown> }>()
  if (!request) return null
  const kind = text(request.kind)
  const persistedState = text(request.state, 'pending')
  const candidate = record(request.candidate)
  const approval = record(request.approval)
  const workflow = record(request.workflow)
  // 合并去重（action=merge）：candidate=保留方（winner），merge 带废弃方与字段 diff。
  const merge = record(request.merge)
  const mergeLoser = record(merge.loser)
  const isMerge = kind === 'candidate_action' && text(request.action) === 'merge'
  const mergeDiff = (list(merge.diff).filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>)
  const expiresAt = Date.parse(text(request.expires_at))
  const expired = persistedState === 'pending' && Number.isFinite(expiresAt) && expiresAt <= Date.now()
  const clientRequestId = text(request.client_request_id)

  const title = kind === 'approval_decision'
    ? `审批决定：${decisionText[text(approval.decision)] || text(approval.decision, '审批决定')}`
    : kind === 'workflow_action'
      ? workflowActionText[text(request.action)] || '工作流动作'
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
      : isMerge
        ? [
            { label: '保留方', value: `${text(candidate.name, '待确认')}（关系 #${text(candidate.id)}）` },
            { label: '废弃方', value: `${text(mergeLoser.name, '待确认')}（关系 #${text(mergeLoser.id)}）` },
            { label: '废弃方阶段', value: text(mergeLoser.stage, '待确认') },
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
        await api.approval(text(approval.approval_id), text(approval.decision), text(request.preflight_token), noteText)
        summary = `已${decisionText[text(approval.decision)] || '处理'}审批：${text(approval.title, text(approval.approval_id))}`
      } else if (kind === 'workflow_action') {
        await api.workflowAction(text(workflow.workflow_id), text(request.action), { note: noteText }, text(request.preflight_token))
        summary = `已执行：${workflowActionText[text(request.action)] || '工作流动作'}`
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
          await api.commit(candidateId, 'merge', text(request.preflight_token), noteText, undefined, loserId)
          summary = `已合并去重：${text(mergeLoser.name, '废弃方')} 已停止并指向 ${text(candidate.name, '保留方')}`
        } else {
          const response = await api.commit(candidateId, text(request.action), text(request.preflight_token), noteText)
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
    return <section className="agent-pending-intent is-closed" aria-label="写入确认已过期"><header><div><small>写入确认</small><b>{title}</b></div></header><p>确认请求已过期（5 分钟有效），未写入 ASA；如需执行，请让 ASA 重新发起。</p></section>
  }
  return <section className="agent-pending-intent" aria-label="写入确认">
    <header><div><small>ASA 发起写入申请</small><b>{title}</b></div><button type="button" aria-label="取消本次写入" onClick={cancelWrite} disabled={busy}><X size={15}/></button></header>
    <dl>{targetLines.map(line => <div key={line.label}><dt>{line.label}</dt><dd>{line.value}</dd></div>)}{noteText && <div><dt>原因</dt><dd>{noteText}</dd></div>}</dl>
    {isMerge && mergeDiff.length > 0 && <dl aria-label="合并字段比对">{mergeDiff.map(item => <div key={text(item.field)}><dt>{text(item.label, '字段')}</dt><dd>{item.same === true ? text(item.winner, '—') : `保留：${text(item.winner, '—')} ｜ 废弃：${text(item.loser, '—')}`}</dd></div>)}</dl>}
    <p>{text(request.impact, '确认后将写入 ASA，并记入统一审计。')}</p>
    {error && <div className="agent-card-error" role="alert">{error}（本次申请已失效，如需执行请让 ASA 重新发起）</div>}
    <footer><button type="button" onClick={cancelWrite} disabled={busy}>取消</button><button type="button" className="primary" disabled={busy || Boolean(error)} onClick={() => void confirmWrite()}>{busy ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}确认执行</button></footer>
  </section>
}

export type InteractionMessage = Pick<AgentMessage, 'understanding_card' | 'execution_receipt' | 'suggested_actions'>
export type InteractionReference = AgentReference
