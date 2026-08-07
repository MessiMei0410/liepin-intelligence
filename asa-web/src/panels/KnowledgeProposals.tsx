import { useCallback, useEffect, useState } from 'react'
import { BookPlus, CircleCheck, LoaderCircle, RefreshCw, Sparkles, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type {
  KnowledgeProposalBrief,
  KnowledgeProposalCandidate,
  KnowledgeProposalDetailPayload,
  KnowledgeProposalEvidence,
  KnowledgeProposalStatus,
} from '../api'
import { date } from '../shared/format'
import { copilotText } from '../shared/text'

// 二期知识飞轮（knowledge_proposal）：Agent 从停止原因聚类/客户反馈/已确认推荐中
// 确定性提出知识增补建议，顾问两段确认（preflight 令牌+签名 → decision）后才写知识文件。
// 本组件负责：
//  1) 提案列表（状态过滤 + 计数徽标）与手动生成（证据不足的聚类如实呈现为"候选"）；
//  2) 展开提案详情（内容 + 可读证据列表）；
//  3) 接受/拒绝两段确认（接受展示影响面再确认；拒绝必须填原因），回执展示并回读刷新。
// 红线：如实反映后端状态（失败不当成功）、不引入 any、不用 prompt/confirm/alert、样式只走 kprop- 前缀。

const statusFilters: Array<{ value: string; label: string }> = [
  { value: 'pending', label: '待确认' },
  { value: 'accepted', label: '已入库' },
  { value: 'rejected', label: '已拒绝' },
  { value: 'superseded', label: '已被取代' },
  { value: 'all', label: '全部' },
]

const sourceTypeLabels: Record<string, string> = {
  stop_reason: '停止原因聚类',
  client_feedback: '客户反馈聚类',
  confirmed_recommendation: '已确认推荐聚类',
}

const formatSample = (sample: Record<string, unknown>) =>
  Object.entries(sample)
    .map(([key, value]) => `${key}：${String(value ?? '')}`)
    .join(' · ')

function EvidenceBlock({ evidence }: { evidence: KnowledgeProposalEvidence[] }) {
  if (!evidence.length) return <p className="kprop-muted">暂无证据记录</p>
  return (
    <div className="kprop-evidence">
      {evidence.map((item, index) => (
        <section key={`ev-${index}`}>
          <b>{sourceTypeLabels[item.source_type || ''] || item.source_type || '证据'}</b>
          {item.summary && <p>{item.summary}</p>}
          {(item.samples || []).length > 0 && (
            <ul>
              {(item.samples || []).slice(0, 5).map((sample, sampleIndex) => (
                <li key={`ev-${index}-s-${sampleIndex}`}>{formatSample(sample)}</li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  )
}

function ProposalDetail({ proposalId, onDecided }: { proposalId: string; onDecided: (nextStatus: string) => void }) {
  const [detail, setDetail] = useState<KnowledgeProposalDetailPayload>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [action, setAction] = useState<'' | 'accept' | 'reject'>('')
  const [impact, setImpact] = useState('')
  const [token, setToken] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [receipt, setReceipt] = useState<{ tone: 'error' | 'success'; text: string }>()

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setDetail(await api.knowledgeProposal(proposalId))
    } catch (e) {
      setError(copilotText(e) || '提案详情加载失败，请重试。')
    } finally {
      setLoading(false)
    }
  }, [proposalId])
  useEffect(() => {
    let alive = true
    api.knowledgeProposal(proposalId)
      .then((payload) => { if (alive) { setDetail(payload); setLoading(false) } })
      .catch((e) => { if (alive) { setError(copilotText(e) || '提案详情加载失败，请重试。'); setLoading(false) } })
    return () => { alive = false }
  }, [proposalId])

  const beginAction = async (kind: 'accept' | 'reject') => {
    if (busy) return
    if (kind === 'reject' && !note.trim()) {
      setReceipt({ tone: 'error', text: '请先填写拒绝原因，再提交。' })
      return
    }
    setBusy(true)
    setReceipt(undefined)
    try {
      if (kind === 'accept' && action !== 'accept') {
        // 第一段：预检，展示影响面，等待顾问二次确认。
        const preflight = await api.preflightKnowledgeProposal(proposalId)
        setToken(preflight.confirmation_token)
        setImpact(preflight.impact || '接受后写入对应知识文件。')
        setAction('accept')
      } else {
        // 第二段（拒绝只需一段：原因已在表单中）：携令牌提交决策。
        const decisionToken = kind === 'accept'
          ? token
          : (await api.preflightKnowledgeProposal(proposalId)).confirmation_token
        const result = await api.decideKnowledgeProposal(proposalId, decisionToken, kind, kind === 'reject' ? note.trim() : '')
        const statusLabel = result.status_label || (kind === 'accept' ? '已入库' : '已拒绝')
        setReceipt({
          tone: 'success',
          text: result.receipt?.idempotent_replay
            ? `该决策此前已提交（${statusLabel}），已同步最新状态。`
            : kind === 'accept'
              ? `提案已确认（${statusLabel}）${result.applied_to ? `，已写入：${result.applied_to}` : ''}。`
              : `提案已拒绝（${statusLabel}），原因已留痕。`,
        })
        setAction('')
        setToken('')
        setNote('')
        await load()
        onDecided(result.status || '')
      }
    } catch (e) {
      setReceipt({ tone: 'error', text: `提案确认失败：${copilotText(e) || '请稍后重试'}。可重新预检后再试。` })
      setAction('')
      setToken('')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div className="kprop-loading"><LoaderCircle className="spin" /><span>提案详情加载中…</span></div>
  if (error) return <div className="kprop-error"><TriangleAlert /><span>{error}</span><button type="button" className="button" onClick={() => { void load() }}>重试</button></div>
  if (!detail) return null
  const content = detail.content || {}
  const pending = detail.status === 'pending'
  return (
    <div className="kprop-detail">
      <section>
        <h4>提案内容</h4>
        {content.rule && <p>{content.rule}</p>}
        {content.rationale && <p>{content.rationale}</p>}
        {content.name && <p>公司：{content.name}</p>}
        {content.occurrences !== undefined && <small>证据出现次数：{content.occurrences}</small>}
        {content.scope && <small>作用域：{content.scope_type === 'client' ? '客户' : content.scope_type || ''}「{content.scope}」</small>}
      </section>
      <section>
        <h4>证据</h4>
        <EvidenceBlock evidence={detail.evidence || []} />
      </section>
      {!pending && (
        <section>
          <h4>处理结果</h4>
          <p>{detail.status_label}{detail.decided_at ? ` · ${date(detail.decided_at)}` : ''}</p>
          {detail.decision_note && <small>原因：{detail.decision_note}</small>}
          {detail.applied_to && <small>写入位置：{detail.applied_to}</small>}
        </section>
      )}
      {pending && (
        <div className="kprop-actions">
          {action === 'accept' ? (
            <div className="kprop-confirm" role="status">
              <p>{impact}</p>
              <div>
                <button type="button" className="button primary" disabled={busy} onClick={() => void beginAction('accept')}>
                  {busy ? <LoaderCircle className="spin" /> : <CircleCheck />}确认接受并入库
                </button>
                <button type="button" className="button" disabled={busy} onClick={() => { setAction(''); setToken(''); setImpact('') }}>取消</button>
              </div>
            </div>
          ) : (
            <button type="button" className="button primary" disabled={busy} onClick={() => void beginAction('accept')}>
              {busy ? <LoaderCircle className="spin" /> : <CircleCheck />}接受并写入知识库
            </button>
          )}
          <label className="kprop-reject-note">
            <span>拒绝原因（拒绝时必填）</span>
            <textarea aria-label="拒绝原因" rows={2} value={note} onChange={(event) => setNote(event.target.value)} placeholder="为什么不采纳这条知识增补建议…" />
          </label>
          <button type="button" className="button danger" disabled={busy} onClick={() => void beginAction('reject')}>
            {busy && action !== 'accept' ? <LoaderCircle className="spin" /> : <TriangleAlert />}拒绝提案
          </button>
        </div>
      )}
      {receipt && (
        <div className={`candidate-action-feedback ${receipt.tone}`} role={receipt.tone === 'error' ? 'alert' : 'status'}>
          {receipt.tone === 'error' ? <TriangleAlert /> : <CircleCheck />}
          <span>{receipt.text}</span>
        </div>
      )}
    </div>
  )
}

export function KnowledgeProposalsPanel() {
  const [status, setStatus] = useState('pending')
  const [items, setItems] = useState<KnowledgeProposalBrief[]>([])
  const [counts, setCounts] = useState<Partial<Record<KnowledgeProposalStatus, number>>>({})
  const [candidates, setCandidates] = useState<KnowledgeProposalCandidate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [openId, setOpenId] = useState('')
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string }>()

  // 事件处理器内的显式刷新（生成/决策回读/重试）：带 loading 态。
  const load = useCallback(async (nextStatus: string) => {
    setLoading(true)
    setError('')
    try {
      const payload = await api.knowledgeProposals(nextStatus)
      setItems(payload.items || [])
      setCounts(payload.counts || {})
    } catch (e) {
      setError(copilotText(e) || '知识增补提案加载失败，请重试。')
    } finally {
      setLoading(false)
    }
  }, [])
  // 挂载与状态过滤切换：alive 模式拉取（不在 effect 体内同步 setState）。
  useEffect(() => {
    let alive = true
    api.knowledgeProposals(status)
      .then((payload) => { if (alive) { setItems(payload.items || []); setCounts(payload.counts || {}); setError(''); setLoading(false) } })
      .catch((e) => { if (alive) { setError(copilotText(e) || '知识增补提案加载失败，请重试。'); setLoading(false) } })
    return () => { alive = false }
  }, [status])

  // 决策后回读：当前过滤已不含该提案时切到决策后的状态，保证展开的详情与回执不被卸载。
  const handleDecided = (nextStatus: string) => {
    if (status !== 'all' && nextStatus && nextStatus !== status) setStatus(nextStatus)
    else void load(status)
  }

  const generate = async () => {
    if (busy) return
    setBusy(true)
    setNotice(undefined)
    try {
      const result = await api.generateKnowledgeProposals()
      setCandidates(result.candidates || [])
      const created = (result.created || []).length
      const existing = (result.existing || []).length
      setNotice({
        tone: 'success',
        text: result.receipt?.idempotent_replay
          ? '本次生成与此前请求相同，返回首次结果。'
          : `扫描完成：新建提案 ${created} 条，已存在 ${existing} 条，证据不足留候选 ${(result.candidates || []).length} 条。`,
      })
      await load(status)
    } catch (e) {
      setNotice({ tone: 'error', text: `提案生成失败：${copilotText(e) || '请稍后重试'}。` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="kprop-panel">
      <header className="kprop-head">
        <div>
          <h3><BookPlus />知识增补提案</h3>
          <p>从停止原因、客户反馈、已确认推荐中沉淀的可增补知识，确认后才写入知识库。</p>
        </div>
        <button type="button" className="button" disabled={busy} onClick={() => void generate()}>
          {busy ? <LoaderCircle className="spin" /> : <Sparkles />}扫描生成提案
        </button>
      </header>
      <div className="kprop-filters" role="tablist" aria-label="提案状态过滤">
        {statusFilters.map((filter) => (
          <button
            key={filter.value}
            type="button"
            role="tab"
            aria-selected={status === filter.value}
            className={status === filter.value ? 'active' : ''}
            onClick={() => { setStatus(filter.value); setOpenId('') }}
          >
            {filter.label}
            {filter.value !== 'all' && counts[filter.value as KnowledgeProposalStatus] !== undefined && (
              <em>{counts[filter.value as KnowledgeProposalStatus]}</em>
            )}
          </button>
        ))}
      </div>
      {notice && (
        <div className={`candidate-action-feedback ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
          {notice.tone === 'error' ? <TriangleAlert /> : <CircleCheck />}
          <span>{notice.text}</span>
        </div>
      )}
      {candidates.length > 0 && (
        <section className="kprop-candidates" aria-label="证据不足的候选">
          <h4>证据不足，仅留候选（未生成提案）</h4>
          <ul>
            {candidates.map((candidate, index) => (
              <li key={`cand-${index}`}>
                <b>{candidate.key || candidate.kind || '候选'}</b>
                <span>{candidate.count ?? 0}/{candidate.needed ?? '-'} · {candidate.reason || ''}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {loading && <div className="kprop-loading"><LoaderCircle className="spin" /><span>提案加载中…</span></div>}
      {error && <div className="kprop-error"><TriangleAlert /><span>{error}</span><button type="button" className="button" onClick={() => void load(status)}><RefreshCw />重试</button></div>}
      {!loading && !error && !items.length && <p className="kprop-muted">当前状态下暂无提案，可点击「扫描生成提案」。</p>}
      {!loading && !error && items.map((item) => (
        <article className="kprop-item" key={item.proposal_id}>
          <button
            type="button"
            className="kprop-item-head"
            aria-expanded={openId === item.proposal_id}
            aria-label={`查看提案：${item.title}`}
            onClick={() => setOpenId(openId === item.proposal_id ? '' : item.proposal_id)}
          >
            <span>
              <b>{item.title}</b>
              <small>{item.proposal_type_label} · {date(item.created_at)}</small>
            </span>
            <em className={`kprop-status kprop-status-${item.status}`}>{item.status_label}</em>
          </button>
          {openId === item.proposal_id && (
            <ProposalDetail proposalId={item.proposal_id} onDecided={handleDecided} />
          )}
        </article>
      ))}
    </div>
  )
}
