import { useState } from 'react'
import { Check, ChevronDown, ChevronUp, LoaderCircle, RotateCcw, X } from 'lucide-react'
import { api } from '../api'
import type { JobFilterNoteBatchCommitResult } from '../api'
import { recordDshConfirmation } from './transport'

const text = (value: unknown, fallback = '') => String(value ?? fallback).trim()
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' ? value as Record<string, unknown> : {}
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : []

// 卡片内直接展示的岗位条数上限：超出折叠为「展开全部 N 条」。
const VISIBLE_LIMIT = 6

// ── 批量口径便签确认卡（filter_note_batch，多岗位一张卡）──────────────────
// 背景：DSH 对多岗位并发发起 N 张单岗位确认卡时，管道一轮只递最后一张
// （confirm_request 单值槽），其余 N-1 张 token 白过期。批量卡一次列出全部
// N 项，用户点一次确认全部生效：确认 → activate（单 token）→ batch commit。
// token 绑定整批 items 的规范化哈希；Core 侧原子落库（任一岗位不存在 → 409
// 全不写）。状态机与单卡一致：pending / confirmed / cancelled / expired /
// drift（409），expired 与 drift 支持「重新预检」换新 token 回到 pending。
export function FilterNoteBatchConfirmCard({ request, sessionId }: { request: Record<string, unknown>; sessionId: string }) {
  const [cancelled, setCancelled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [result, setResult] = useState<{ summary: string; receipt?: Record<string, unknown>; commit?: JobFilterNoteBatchCommitResult }>()
  // 重预检签发的新 token/过期时间（expired 与 409 漂移共用）；确认仍用最新一份。
  const [refreshed, setRefreshed] = useState<{ token: string; expiresAt: string } | null>(null)
  const [repreflightBusy, setRepreflightBusy] = useState(false)

  const persistedState = text(request.state, 'pending')
  const items = (list(request.items).filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>)
  const title = `批量保存口径便签（${items.length} 个岗位）`
  const activeToken = refreshed?.token || text(request.preflight_token)
  const expiresAt = Date.parse(refreshed?.expiresAt || text(request.expires_at))
  const expired = persistedState === 'pending' && Number.isFinite(expiresAt) && expiresAt <= Date.now()
  const clientRequestId = text(request.client_request_id)

  const jobLabel = (item: Record<string, unknown>) => {
    const job = record(item.job)
    return [text(job.client), text(job.title)].filter(Boolean).join(' / ') || `岗位 #${text(item.job_id)}`
  }
  const commitItems = () => items.map(item => ({ job_id: Number(item.job_id), note: text(item.note) }))

  const backfill = (state: 'confirmed' | 'cancelled', summary: string, receipt?: Record<string, unknown>) => {
    void recordDshConfirmation(sessionId, clientRequestId, { state, summary, ...(receipt ? { execution_receipt: receipt } : {}) })
  }
  const confirmWrite = async () => {
    if (busy) return
    if (!items.length || items.some(item => !Number.isFinite(Number(item.job_id)) || !text(item.note))) {
      setError('确认请求缺少岗位或便签内容')
      return
    }
    setBusy(true); setError('')
    try {
      const response = await api.jobFilterNoteBatchCommit(commitItems(), activeToken)
      const summary = response.already_saved > 0
        ? `已保存 ${response.saved} 个岗位的口径便签（${response.already_saved} 个此前已保存，未重复写入）——出名单时将随口径声明显示`
        : `已保存 ${response.saved} 个岗位的口径便签——出名单时将随口径声明显示`
      const receipt = {
        version: 'execution_receipt_v1', state: '已完成', summary,
        succeeded: response.saved, skipped: response.already_saved, failed: 0, verified: true,
      }
      setResult({ summary, receipt, commit: response })
      backfill('confirmed', summary, receipt)
    } catch (value) {
      // 409 漂移（token 过期/已用、岗位不存在、items 被篡改）：展示服务端中文 detail，不重试。
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
  // 重新预检：用卡片自身携带的 items 走 batch-preflight 换新 token，回到 pending
  // 待确认。重预检只换 token，不执行写入；确认动作仍由人点「确认执行」。
  const repreflight = async () => {
    if (busy || repreflightBusy) return
    if (!items.length || items.some(item => !Number.isFinite(Number(item.job_id)) || !text(item.note))) {
      setError('确认请求缺少岗位或便签内容')
      return
    }
    setRepreflightBusy(true); setError('')
    try {
      const response = await api.jobFilterNoteBatchPreflight(commitItems())
      setRefreshed({ token: response.token, expiresAt: text(response.expires_at) })
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value))
    } finally {
      setRepreflightBusy(false)
    }
  }
  const repreflightButton = <button type="button" disabled={busy || repreflightBusy} onClick={() => void repreflight()}>{repreflightBusy ? <LoaderCircle className="spin" size={14}/> : <RotateCcw size={14}/>}重新预检</button>

  if (result) {
    const commit = result.commit
    return <section className="agent-execution-receipt verified" aria-label="写入执行回执">
      <div><b>执行回执</b><strong>已完成</strong></div>
      <p>{result.summary}</p>
      {commit && <ul aria-label="逐项结果">{commit.results.map(item => {
        const source = items.find(entry => Number(entry.job_id) === Number(item.job_id))
        return <li key={item.job_id}>{source ? jobLabel(source) : `岗位 #${item.job_id}`}：{item.already_saved ? '此前已保存' : '已保存'}</li>
      })}</ul>}
      <small>已完成服务端写入</small>
    </section>
  }
  if (persistedState === 'confirmed') {
    return <section className="agent-execution-receipt verified" aria-label="写入执行回执"><div><b>执行回执</b><strong>已完成</strong></div><p>{text(request.result_summary, '该批量写入已确认并同步到 ASA')}</p><small>已完成服务端写入</small></section>
  }
  if (persistedState === 'cancelled' || cancelled) {
    return <section className="agent-pending-intent is-closed" aria-label="写入确认已取消"><header><div><small>写入确认</small><b>{title}</b></div></header><p>已取消，未写入 ASA。</p></section>
  }
  if (expired) {
    return <section className="agent-pending-intent is-closed" aria-label="写入确认已过期"><header><div><small>写入确认</small><b>{title}</b></div></header><p>确认请求已过期（5 分钟有效），未写入 ASA；可直接重新预检后继续，无需重新下指令。</p>{error && <div className="agent-card-error" role="alert">{error}</div>}<footer><button type="button" onClick={cancelWrite} disabled={busy || repreflightBusy}>取消</button>{repreflightButton}</footer></section>
  }
  const visibleItems = expanded ? items : items.slice(0, VISIBLE_LIMIT)
  return <section className="agent-pending-intent" aria-label="写入确认">
    <header><div><small>ASA 发起写入申请</small><b>{title}</b></div><button type="button" aria-label="取消本次写入" onClick={cancelWrite} disabled={busy}><X size={15}/></button></header>
    <dl aria-label="岗位口径便签清单">{visibleItems.map(item => {
      const previous = text(item.previous_note)
      return <div key={text(item.job_id)}>
        <dt>{jobLabel(item)}</dt>
        <dd>{text(item.note)}{previous && <small>（当前：{previous}）</small>}</dd>
      </div>
    })}</dl>
    {items.length > VISIBLE_LIMIT && <button type="button" onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronUp size={14}/> : <ChevronDown size={14}/>}{expanded ? '收起' : `展开全部 ${items.length} 条`}</button>}
    <p>{text(request.impact, `确认后将一次性保存 ${items.length} 个岗位的口径便签，并记入统一审计。`)}</p>
    {error && <div className="agent-card-error" role="alert">{error}</div>}
    <footer><button type="button" onClick={cancelWrite} disabled={busy || repreflightBusy}>取消</button>{error && repreflightButton}<button type="button" className="primary" disabled={busy || repreflightBusy || Boolean(error)} onClick={() => void confirmWrite()}>{busy ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}确认执行</button></footer>
  </section>
}
