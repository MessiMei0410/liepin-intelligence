import { useState } from 'react'
import { Check, ChevronRight, CircleAlert, LoaderCircle, RotateCcw, Search, X } from 'lucide-react'
import { api } from '../api'
import type { AgentMessage } from './sessionModel'
import type { AgentReference } from './transport'

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
  return <section className={`agent-execution-receipt ${verified ? 'verified' : ''}`} aria-label="执行回执"><div><b>执行回执</b><strong>{state}</strong></div><p>{text(receipt.summary, '尚未执行写入或外部动作')}</p>{counts.length > 0 && <div className="agent-receipt-counts">{counts.map(item => <span key={item.label}>{item.label} <b>{String(item.value)}</b></span>)}</div>}<small>{verified ? '已完成服务端回查' : '等待真实执行结果'}{text(scope.label || scope.type) && ` · 范围 ${text(scope.label || scope.type)}`}{text(receipt.failure_reason) && ` · ${text(receipt.failure_reason)}`}{reasons.length > 0 && ` · ${reasons.join('；')}`}</small>{text(receipt.next_step) && <p className="agent-understanding-next"><ChevronRight size={14}/>{text(receipt.next_step)}</p>}</section>
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

export type InteractionMessage = Pick<AgentMessage, 'understanding_card' | 'execution_receipt' | 'suggested_actions'>
export type InteractionReference = AgentReference
