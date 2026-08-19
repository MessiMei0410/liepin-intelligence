import { useCallback, useEffect, useRef, useState } from 'react'
import { Radar, Send, UsersRound } from 'lucide-react'
import type { Job } from '../api'
import { useDialogFocus } from '../shared/useDialogFocus'
import { attachDialogDrag, attachDialogResize } from '../shared/dialogDragResize'

// S7-2 雷达联动最小闭环：最新榜单 → 「发起 Mapping」（trigger=radar 由后端锚定）+
// 「库里有这些人」激活清单弹层。文案业务语言：雷达信号=「公司近况信号」。
// 自包含 fetch 封装（不动 src/api.ts）；榜单只出建议，所有对外动作由顾问本人执行。

type RadarSignal = { company: string; type: string; summary: string; implication?: string; as_of: string; source_urls: string[]; confidence: string; linked_action: string }
type RadarRanking = { company: string; score: number; reason: string; suggested_action?: string; signal_count?: number; related_jobs?: string[] }
type RadarScan = { scan_date: string; signals: RadarSignal[]; ranking: RadarRanking[]; stats: Record<string, number> }
type ActivateItem = { id: number; name_masked: string; current_title: string; current_company: string; tenure: string; stage: string; last_action_at: string }
type LoadState = 'loading' | 'ready' | 'error'

const SIGNAL_TYPE_LABELS: Record<string, string> = {
  earnings: '财报/业绩预告', funding: '融资/IPO 进展', equity: '股权激励/员工持股',
  org_change: '组织/高管变动', hiring: '招聘异动', risk: '风险事件',
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || payload.detail || `请求失败（${response.status}）`)
  return payload as T
}

const mappingRequestId = (company: string, jobId: number, sequence: number) =>
  `radar-ui-${company}-${jobId}-${Date.now()}-${sequence}`

export function RadarPage({ jobs }: { jobs: Job[] }) {
  const [scan, setScan] = useState<RadarScan>()
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  const [jobByCompany, setJobByCompany] = useState<Record<string, number>>({})
  const [busyCompany, setBusyCompany] = useState('')
  const [activateCompany, setActivateCompany] = useState('')
  const [activateItems, setActivateItems] = useState<ActivateItem[]>([])
  const [activateState, setActivateState] = useState<LoadState>('loading')
  const [activateError, setActivateError] = useState('')
  const requestSequence = useRef(0)
  const activateRequestSequence = useRef(0)
  const activateDialogRef = useDialogFocus<HTMLDivElement>(Boolean(activateCompany))
  const radarDialogRef = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    setLoadState('loading'); setLoadError('')
    try {
      const payload = await request<{ radar_scan: RadarScan }>('/api/v1/radar/scans/latest')
      setScan(payload.radar_scan); setLoadState('ready')
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e)); setLoadState('error')
    }
  }, [])
  useEffect(() => { queueMicrotask(() => void load()) }, [load])

  const startMapping = async (company: string) => {
    const jobId = jobByCompany[company]
    if (!jobId) { setActionError(`请先为「${company}」选择一个在手岗位`); return }
    setBusyCompany(company); setActionError(''); setNotice('')
    try {
      requestSequence.current += 1
      const requestId = mappingRequestId(company, jobId, requestSequence.current)
      const payload = await request<{ already_exists: boolean; artifact_id: string; note?: string }>(
        '/api/v1/radar/scans/latest/actions/start-mapping',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestId },
          body: JSON.stringify({ request_id: requestId, company, job_id: jobId }),
        },
      )
      setNotice(payload.already_exists
        ? `今天已对「${company}」发起过 Mapping，任务卡：${payload.artifact_id}`
        : `已为「${company}」建好 Mapping 任务卡：${payload.artifact_id}（名单仅供顾问本人决策，系统不自动触达）`)
    } catch (e) { setActionError(e instanceof Error ? e.message : String(e)) }
    finally { setBusyCompany('') }
  }

  const loadActivate = async (company: string) => {
    const sequence = ++activateRequestSequence.current
    setActivateState('loading'); setActivateError('')
    try {
      const payload = await request<{ candidates: ActivateItem[] }>(
        `/api/v1/radar/scans/latest/actions/activate?company=${encodeURIComponent(company)}`,
      )
      if (sequence !== activateRequestSequence.current) return
      setActivateItems(payload.candidates); setActivateState('ready')
    } catch (e) {
      if (sequence !== activateRequestSequence.current) return
      setActivateError(e instanceof Error ? e.message : String(e)); setActivateState('error')
    }
  }

  const closeActivate = useCallback(() => {
    activateRequestSequence.current += 1
    setActivateCompany('')
  }, [])

  const openActivate = (company: string) => {
    setActivateCompany(company); setActivateItems([])
    void loadActivate(company)
  }

  useEffect(() => {
    if (!activateCompany) return
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') closeActivate() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [activateCompany, closeActivate])

  // 雷达激活清单弹窗：统一拖动/缩放实现。
  useEffect(() => {
    if (!activateCompany) return
    const el = radarDialogRef.current
    if (!el) return
    const header = el.querySelector<HTMLElement>('header')
    const cleanupDrag = attachDialogDrag(el, { header })
    const cleanupResize = attachDialogResize(el, { minWidth: 280, minHeight: 200 })
    return () => {
      cleanupDrag()
      cleanupResize()
    }
  }, [activateCompany])

  if (!scan && loadState === 'error') return <div className="empty" role="alert">
    <p>{loadError || '人才雷达加载失败，请稍后重试。'}</p>
    <button className="button" onClick={() => void load()}>重新加载人才雷达</button>
  </div>
  if (!scan) return <div className="empty" role="status">正在读取本周人才流动雷达榜单…</div>
  const ranking = scan.ranking || []
  // 数据陈旧可见提示（P10）：周度扫描未运行时榜单静默停留旧日期，这里显式告警，
  // 不再让"本周榜单"悄无声息地过期。扫描日期距今超过 7 天即视为陈旧。
  const scanTimestamp = new Date(scan.scan_date.includes('T') ? scan.scan_date : `${scan.scan_date}T00:00:00`).getTime()
  const scanAgeDays = Number.isFinite(scanTimestamp) ? Math.floor((Date.now() - scanTimestamp) / 86_400_000) : 0
  const scanStale = scanAgeDays > 7
  const signalsByCompany = (scan.signals || []).reduce<Record<string, RadarSignal[]>>((acc, signal) => {
    (acc[signal.company] ||= []).push(signal); return acc
  }, {})

  return <div>
    {scanStale && <p role="status" className="radar-stale-warning" style={{ margin: '0 0 12px', padding: '9px 10px', background: 'var(--amber-soft, #fff5d6)', color: 'var(--amber, #8a6500)', borderRadius: 6, fontSize: 12, lineHeight: 1.6 }}>
      榜单已 {scanAgeDays} 天未更新：周度自动扫描当前未在运行，以下信号截至 {scan.scan_date}，可能已过时。恢复扫描后榜单会自动刷新。
    </p>}
    <p style={{ color: 'var(--muted, #6b7a72)', margin: '0 0 12px' }}>
      {scanStale ? `榜单（${scan.scan_date}，已过期）` : `本周榜单（${scan.scan_date}）`}：信号全部来自公开信息，「可能意味着」是推测，仅供顾问本人判断；系统不自动触达任何人选。
    </p>
    {notice && <div className="toast" role="status" style={{ position: 'static', marginBottom: 12 }}>{notice}</div>}
    {actionError && <div className="toast" role="alert" style={{ position: 'static', marginBottom: 12 }}>{actionError}</div>}
    {!ranking.length && <div className="empty">
      <p style={{ margin: '0 0 8px' }}>本周未发现达到上榜强度的公开信号。</p>
      <button className="button" onClick={() => void load()}>重新加载人才雷达</button>
    </div>}
    {ranking.map((entry, index) => <section className="card" key={entry.company} style={{ marginBottom: 12, padding: 12 }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Radar size={16} />
        <strong>{index + 1}. {entry.company}</strong>
        <small className="radar-reason" style={{ color: 'var(--muted, #6b7a72)' }}>信号强度 {entry.score} · {entry.reason}</small>
      </header>
      <ul style={{ margin: '8px 0', paddingLeft: 18 }}>
        {(signalsByCompany[entry.company] || []).map((signal, i) => <li key={i} className="radar-signal" style={{ marginBottom: 4 }}>
          <b>【{SIGNAL_TYPE_LABELS[signal.type] || signal.type}】</b>{signal.summary}（{signal.as_of}）
          {signal.implication ? <span style={{ color: 'var(--muted, #6b7a72)' }}>｜可能意味着：{signal.implication}（推测）</span> : null}
          {' '}<a href={signal.source_urls[0]} target="_blank" rel="noreferrer">来源</a>
        </li>)}
      </ul>
      <footer style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <select
          aria-label={`选择要联动 Mapping 的岗位（${entry.company}）`}
          value={jobByCompany[entry.company] || ''}
          onChange={e => setJobByCompany(prev => ({ ...prev, [entry.company]: Number(e.target.value) }))}
        >
          <option value="">选择在手岗位…</option>
          {jobs.map(job => <option key={job.id} value={job.id}>{job.client} · {job.title}</option>)}
        </select>
        <button className="button" disabled={busyCompany === entry.company} onClick={() => void startMapping(entry.company)}>
          <Send size={14} />{busyCompany === entry.company ? '发起中…' : '发起 Mapping'}
        </button>
        <button className="button" onClick={() => openActivate(entry.company)}>
          <UsersRound size={14} />库里有这些人
        </button>
      </footer>
    </section>)}
    {activateCompany && <div ref={activateDialogRef}
      role="dialog" aria-modal="true" aria-label={`库里在 ${activateCompany} 的人`}
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}
      onClick={closeActivate}
    >
      <div ref={radarDialogRef} className="card radar-dialog" style={{ width: 'min(720px, calc(100vw - 24px))', maxHeight: '70vh', overflow: 'auto', padding: 16 }} onClick={e => e.stopPropagation()}>
        <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'grab', userSelect: 'none', touchAction: 'none' }} title="按住拖动；右下角可缩放">
          <strong>库里有这些人 · {activateCompany}</strong>
          <button className="button" onClick={closeActivate}>关闭</button>
        </header>
        <p style={{ color: 'var(--muted, #6b7a72)' }}>清单只读展示；是否触达、怎么触达由顾问本人决定，系统不自动触达。</p>
        {activateState === 'loading' && <div className="empty" role="status">正在读取人才库人选…</div>}
        {activateState === 'error' && <div className="empty" role="alert">
          <p>{activateError || '人选清单加载失败，请稍后重试。'}</p>
          <button className="button" onClick={() => void loadActivate(activateCompany)}>重新加载人选</button>
        </div>}
        {activateState === 'ready' && !activateItems.length && <div className="empty">人才库里暂时没有这家公司的人选。</div>}
        {activateState === 'ready' && activateItems.map(item => <div key={`${item.tenure}-${item.id}`} className="radar-activate-item" style={{ display: 'flex', gap: 10, padding: '8px 0', borderTop: '1px solid var(--line, #e3eae6)' }}>
          <b>{item.name_masked}</b>
          <span>{item.current_title}</span>
          <small style={{ color: 'var(--muted, #6b7a72)' }}>
            {item.tenure} · {item.stage || '未入流程'} · 最近动作 {item.last_action_at || '—'}
          </small>
        </div>)}
      </div>
    </div>}
  </div>
}
