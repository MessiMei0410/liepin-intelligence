import { useCallback, useEffect, useState } from 'react'
import { Building2, CircleCheck, LoaderCircle, RefreshCw, Search, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type {
  CompanyCalibrationProgress,
  CompanyCalibrationQueueItem,
  CompanyCalibrationStatus,
} from '../api'
import { copilotText } from '../shared/text'

// 二期知识飞轮：核心公司校准（company_calibration）。顾问逐公司确认/修正图谱条目
// （行业/产品线/技能标签/职级体系/禁挖竞业标记/备注），校准持久化在 DB 覆盖层，
// 图谱 JSON 保持原始名单。本组件负责：
//  1) 待校准队列（未校准优先；搜索名称/赛道/主营业务；状态过滤）；
//  2) 逐公司校准表单（字段与后端校准模型一一对应；图谱原值作为初始值）；
//  3) 进度指示（已校准 N/目标 50）；
//  4) 提交回执（版本/changed/幂等重放如实呈现）+ 提交后回读刷新队列与进度。
// 红线：如实反映后端状态、不引入 any、不用 prompt/confirm/alert、样式只走 ccal- 前缀。

const statusFilters: Array<{ value: string; label: string }> = [
  { value: '', label: '待校准' },
  { value: 'pending', label: '未校准' },
  { value: 'needs_review', label: '待复核' },
  { value: 'calibrated', label: '已校准' },
  { value: 'rejected', label: '已拒绝' },
  { value: 'all', label: '全部' },
]

const splitLines = (value: string) =>
  value.split(/[\n、,，;；]/).map(item => item.trim()).filter(Boolean)

type CalibrationFormProps = {
  item: CompanyCalibrationQueueItem
  onSubmitted: (receipt: { tone: 'error' | 'success'; text: string }) => void
}

function CalibrationForm({ item, onSubmitted }: CalibrationFormProps) {
  const existing = item.calibration
  const [track, setTrack] = useState(existing?.track ?? item.track)
  const [productLines, setProductLines] = useState((existing?.product_lines ?? []).join('\n'))
  const [skillTags, setSkillTags] = useState((existing?.skill_tags ?? item.categories).join('\n'))
  const [levelSystem, setLevelSystem] = useState(existing?.level_system ?? '')
  const [noPoach, setNoPoach] = useState(existing?.no_poach ?? false)
  const [nonCompete, setNonCompete] = useState(existing?.non_compete ?? false)
  const [note, setNote] = useState(existing?.note ?? '')
  const [busy, setBusy] = useState<'' | CompanyCalibrationStatus>('')
  const [feedback, setFeedback] = useState<{ tone: 'error' | 'success'; text: string }>()

  const submit = async (status: CompanyCalibrationStatus) => {
    if (busy) return
    if (status === 'rejected' && !note.trim()) {
      setFeedback({ tone: 'error', text: '拒绝该校准条目时请先在备注里写明原因。' })
      return
    }
    setBusy(status)
    setFeedback(undefined)
    try {
      const result = await api.submitCompanyCalibration({
        company_name: item.company_name,
        status,
        track: track.trim(),
        product_lines: splitLines(productLines),
        skill_tags: splitLines(skillTags),
        level_system: levelSystem.trim(),
        no_poach: noPoach,
        non_compete: nonCompete,
        note: note.trim(),
      })
      const statusLabel = result.status_label || status
      const text = result.receipt?.idempotent_replay
        ? `该校准此前已提交（${statusLabel}，v${result.version ?? '-'}），已同步最新状态。`
        : result.changed === false
          ? `内容与已存校准一致（${statusLabel}，v${result.version ?? '-'}），未产生新版本。`
          : `已保存「${result.company_name || item.company_name}」校准（${statusLabel}，v${result.version ?? 1}）。`
      setFeedback({ tone: 'success', text })
      onSubmitted({ tone: 'success', text })
    } catch (e) {
      setFeedback({ tone: 'error', text: `校准提交失败：${copilotText(e) || '请稍后重试'}。` })
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="ccal-form">
      <div className="ccal-grid">
        <label>
          <span>行业（赛道）</span>
          <input aria-label="行业（赛道）" value={track} onChange={event => setTrack(event.target.value)} />
        </label>
        <label>
          <span>职级体系</span>
          <input aria-label="职级体系" value={levelSystem} onChange={event => setLevelSystem(event.target.value)} placeholder="如：P/M 序列、T 序列…" />
        </label>
        <label>
          <span>产品线（每行一条）</span>
          <textarea aria-label="产品线" rows={3} value={productLines} onChange={event => setProductLines(event.target.value)} />
        </label>
        <label>
          <span>技能标签（每行一条）</span>
          <textarea aria-label="技能标签" rows={3} value={skillTags} onChange={event => setSkillTags(event.target.value)} />
        </label>
      </div>
      <div className="ccal-flags">
        <label><input type="checkbox" checked={noPoach} onChange={event => setNoPoach(event.target.checked)} />禁挖（在职保护）</label>
        <label><input type="checkbox" checked={nonCompete} onChange={event => setNonCompete(event.target.checked)} />竞业限制</label>
      </div>
      <label className="ccal-note">
        <span>备注{existing ? `（当前 v${existing.version} · ${existing.status_label}）` : ''}</span>
        <textarea aria-label="校准备注" rows={2} value={note} onChange={event => setNote(event.target.value)} placeholder="校准依据、待确认点；拒绝时必填原因…" />
      </label>
      <div className="ccal-actions">
        <button type="button" className="button primary" disabled={!!busy} onClick={() => void submit('calibrated')}>
          {busy === 'calibrated' ? <LoaderCircle className="spin" /> : <CircleCheck />}提交校准
        </button>
        <button type="button" className="button" disabled={!!busy} onClick={() => void submit('needs_review')}>
          {busy === 'needs_review' ? <LoaderCircle className="spin" /> : null}标记待复核
        </button>
        <button type="button" className="button danger" disabled={!!busy} onClick={() => void submit('rejected')}>
          {busy === 'rejected' ? <LoaderCircle className="spin" /> : <TriangleAlert />}拒绝（不进消费）
        </button>
      </div>
      {feedback && (
        <div className={`candidate-action-feedback ${feedback.tone}`} role={feedback.tone === 'error' ? 'alert' : 'status'}>
          {feedback.tone === 'error' ? <TriangleAlert /> : <CircleCheck />}
          <span>{feedback.text}</span>
        </div>
      )}
    </div>
  )
}

export function CompanyCalibrationPanel() {
  const [items, setItems] = useState<CompanyCalibrationQueueItem[]>([])
  const [progress, setProgress] = useState<CompanyCalibrationProgress>()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [openKey, setOpenKey] = useState('')
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string }>()

  const load = useCallback(async (nextQuery: string, nextStatus: string) => {
    setLoading(true)
    setError('')
    try {
      const [queue, progressPayload] = await Promise.all([
        api.companyCalibrations(nextQuery, nextStatus),
        api.companyCalibrationProgress(),
      ])
      setItems(queue.items || [])
      setProgress(progressPayload)
    } catch (e) {
      setError(copilotText(e) || '校准队列加载失败，请重试。')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => { queueMicrotask(() => void load(query, status)) }, [query, status, load])

  // 提交后回读：刷新队列与进度；当前过滤已不含该公司时收起表单。
  const handleSubmitted = (receipt: { tone: 'error' | 'success'; text: string }) => {
    setNotice(receipt)
    setOpenKey('')
    void load(query, status)
  }

  const target = progress?.target ?? 50
  const calibrated = progress?.calibrated ?? 0
  const ratio = Math.min(1, target > 0 ? calibrated / target : 0)

  return (
    <div className="ccal-panel">
      <header className="ccal-head">
        <div>
          <h3><Building2 />核心公司校准</h3>
          <p>逐公司确认/修正图谱条目（行业/产品线/技能标签/职级体系/禁挖竞业），校准值优先于原始名单进入策略与评估。</p>
        </div>
        <div className="ccal-progress" role="status" aria-label={`校准进度：已校准 ${calibrated} 家，目标 ${target} 家`}>
          <b>已校准 {calibrated}/{target}</b>
          <div className="ccal-progress-bar"><i style={{ width: `${Math.round(ratio * 100)}%` }} /></div>
          {progress && (
            <small>图谱共 {progress.total} 家 · 未校准 {progress.pending} · 待复核 {progress.needs_review} · 已拒绝 {progress.rejected}</small>
          )}
        </div>
      </header>
      <div className="ccal-toolbar">
        <label className="ccal-search">
          <Search />
          <input
            aria-label="搜索公司"
            value={query}
            onChange={event => { setQuery(event.target.value); setOpenKey('') }}
            placeholder="搜索公司名称、赛道或主营业务…"
          />
        </label>
        <div className="ccal-filters" role="tablist" aria-label="校准状态过滤">
          {statusFilters.map(filter => (
            <button
              key={filter.value || 'default'}
              type="button"
              role="tab"
              aria-selected={status === filter.value}
              className={status === filter.value ? 'active' : ''}
              onClick={() => { setStatus(filter.value); setOpenKey('') }}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>
      {notice && (
        <div className={`candidate-action-feedback ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
          {notice.tone === 'error' ? <TriangleAlert /> : <CircleCheck />}
          <span>{notice.text}</span>
        </div>
      )}
      {loading && <div className="ccal-loading"><LoaderCircle className="spin" /><span>校准队列加载中…</span></div>}
      {error && (
        <div className="ccal-error">
          <TriangleAlert /><span>{error}</span>
          <button type="button" className="button" onClick={() => void load(query, status)}><RefreshCw />重试</button>
        </div>
      )}
      {!loading && !error && !items.length && (
        <p className="ccal-muted">当前过滤下没有待校准公司{query ? '，可调整搜索词或状态过滤' : '，本期校准已完成'}。</p>
      )}
      {!loading && !error && items.map(item => (
        <article className="ccal-item" key={item.company_key}>
          <button
            type="button"
            className="ccal-item-head"
            aria-expanded={openKey === item.company_key}
            aria-label={`校准公司：${item.company_name}`}
            onClick={() => setOpenKey(openKey === item.company_key ? '' : item.company_key)}
          >
            <span>
              <b>{item.company_name}</b>
              <small>{[item.track, item.business].filter(Boolean).join(' · ') || '图谱暂无赛道/主营业务'}</small>
            </span>
            <em className={`ccal-status ccal-status-${item.status}`}>{item.status_label}</em>
          </button>
          {openKey === item.company_key && (
            <CalibrationForm key={`${item.company_key}:${item.calibration?.version ?? 0}`} item={item} onSubmitted={handleSubmitted} />
          )}
        </article>
      ))}
    </div>
  )
}
