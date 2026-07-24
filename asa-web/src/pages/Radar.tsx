import { useCallback, useEffect, useState } from 'react'
import { Radar, Send, UsersRound } from 'lucide-react'
import { Job } from '../api'

// S7-2 雷达联动最小闭环：最新榜单 → 「发起 Mapping」（trigger=radar 由后端锚定）+
// 「库里有这些人」激活清单弹层。文案业务语言：雷达信号=「公司近况信号」。
// 自包含 fetch 封装（不动 src/api.ts）；榜单只出建议，所有对外动作由顾问本人执行。

type RadarSignal = { company: string; type: string; summary: string; implication?: string; as_of: string; source_urls: string[]; confidence: string; linked_action: string }
type RadarRanking = { company: string; score: number; reason: string; suggested_action?: string; signal_count?: number; related_jobs?: string[] }
type RadarScan = { scan_date: string; signals: RadarSignal[]; ranking: RadarRanking[]; stats: Record<string, number> }
type ActivateItem = { id: number; name_masked: string; current_title: string; current_company: string; tenure: string; stage: string; last_action_at: string }

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

export function RadarPage({ jobs }: { jobs: Job[] }) {
  const [scan, setScan] = useState<RadarScan>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [jobByCompany, setJobByCompany] = useState<Record<string, number>>({})
  const [busyCompany, setBusyCompany] = useState('')
  const [activateCompany, setActivateCompany] = useState('')
  const [activateItems, setActivateItems] = useState<ActivateItem[]>([])
  const [activateError, setActivateError] = useState('')

  const load = useCallback(async () => {
    try {
      const payload = await request<{ radar_scan: RadarScan }>('/api/v1/radar/scans/latest')
      setScan(payload.radar_scan); setError('')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }, [])
  useEffect(() => { void load() }, [load])

  const startMapping = async (company: string) => {
    const jobId = jobByCompany[company]
    if (!jobId) { setError(`请先为「${company}」选择一个在手岗位`); return }
    setBusyCompany(company); setError(''); setNotice('')
    try {
      const payload = await request<{ already_exists: boolean; artifact_id: string; note?: string }>(
        '/api/v1/radar/scans/latest/actions/start-mapping',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `radar-ui-${company}-${jobId}-${Date.now()}` },
          body: JSON.stringify({ request_id: `radar-ui-${Date.now()}`, company, job_id: jobId }),
        },
      )
      setNotice(payload.already_exists
        ? `今天已对「${company}」发起过 Mapping，任务卡：${payload.artifact_id}`
        : `已为「${company}」建好 Mapping 任务卡：${payload.artifact_id}（名单仅供顾问本人决策，系统不自动触达）`)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusyCompany('') }
  }

  const openActivate = async (company: string) => {
    setActivateCompany(company); setActivateItems([]); setActivateError('')
    try {
      const payload = await request<{ candidates: ActivateItem[] }>(
        `/api/v1/radar/scans/latest/actions/activate?company=${encodeURIComponent(company)}`,
      )
      setActivateItems(payload.candidates)
    } catch (e) { setActivateError(e instanceof Error ? e.message : String(e)) }
  }

  if (error && !scan) return <div className="empty">{error}</div>
  if (!scan) return <div className="empty">正在读取本周人才流动雷达榜单…</div>
  const ranking = scan.ranking || []
  const signalsByCompany = (scan.signals || []).reduce<Record<string, RadarSignal[]>>((acc, signal) => {
    (acc[signal.company] ||= []).push(signal); return acc
  }, {})

  return <div>
    <p style={{ color: 'var(--muted, #6b7a72)', margin: '0 0 12px' }}>
      本周榜单（{scan.scan_date}）：信号全部来自公开信息，「可能意味着」是推测，仅供顾问本人判断；系统不自动触达任何人选。
    </p>
    {notice && <div className="toast" style={{ position: 'static', marginBottom: 12 }}>{notice}</div>}
    {error && <div className="toast" style={{ position: 'static', marginBottom: 12 }}>{error}</div>}
    {!ranking.length && <div className="empty">本周未发现达到上榜强度的公开信号。</div>}
    {ranking.map((entry, index) => <section className="card" key={entry.company} style={{ marginBottom: 12, padding: 12 }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Radar size={16} />
        <strong>{index + 1}. {entry.company}</strong>
        <small style={{ color: 'var(--muted, #6b7a72)' }}>信号强度 {entry.score} · {entry.reason}</small>
      </header>
      <ul style={{ margin: '8px 0', paddingLeft: 18 }}>
        {(signalsByCompany[entry.company] || []).map((signal, i) => <li key={i} style={{ marginBottom: 4 }}>
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
        <button className="button" onClick={() => void openActivate(entry.company)}>
          <UsersRound size={14} />库里有这些人
        </button>
      </footer>
    </section>)}
    {activateCompany && <div
      role="dialog" aria-label={`库里在 ${activateCompany} 的人`}
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}
      onClick={() => setActivateCompany('')}
    >
      <div className="card" style={{ minWidth: 520, maxWidth: 720, maxHeight: '70vh', overflow: 'auto', padding: 16 }} onClick={e => e.stopPropagation()}>
        <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <strong>库里有这些人 · {activateCompany}</strong>
          <button className="button" onClick={() => setActivateCompany('')}>关闭</button>
        </header>
        <p style={{ color: 'var(--muted, #6b7a72)' }}>清单只读展示；是否触达、怎么触达由顾问本人决定，系统不自动触达。</p>
        {activateError && <div className="empty">{activateError}</div>}
        {!activateError && !activateItems.length && <div className="empty">人才库里暂时没有这家公司的人选。</div>}
        {activateItems.map(item => <div key={`${item.tenure}-${item.id}`} style={{ display: 'flex', gap: 10, padding: '8px 0', borderTop: '1px solid var(--line, #e3eae6)' }}>
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
