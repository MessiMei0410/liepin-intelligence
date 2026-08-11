import { useEffect, useRef } from 'react'
import { ExternalLink, LoaderCircle, RefreshCw, Users, X } from 'lucide-react'
import type { CandidateListCardData } from '../workflows/CandidateListCard'
import { candidateStageTone } from '../workflows/CandidateListCard'
import { attachDialogDrag, attachDialogResize, type DragResizeAnchor } from '../shared/dialogDragResize'

const MIN_DIALOG_W = 320
const MIN_DIALOG_H = 240

function nativeBridge(type: string, payload: Record<string, unknown>): boolean {
  const handler = (window as unknown as { webkit?: { messageHandlers?: { asaNative?: { postMessage: (msg: unknown) => void } } } }).webkit?.messageHandlers?.asaNative
  if (!handler) return false
  handler.postMessage({ type, ...payload })
  return true
}

export function CandidateListDialog({
  data,
  onOpenCandidate,
  onOpenJob,
  onClose,
  onRefresh,
  refreshing = false,
}: {
  data: CandidateListCardData
  onOpenCandidate: (jobCandidateId: number) => void
  onOpenJob?: (jobId: number) => void
  onClose: () => void
  onRefresh?: () => void
  refreshing?: boolean
}) {
  const dialogRef = useRef<HTMLElement>(null)
  const summary = data.summary || {}
  const groups = Array.isArray(data.groups) ? data.groups : []
  const total = Number(summary.total ?? groups.reduce((sum, group) => sum + (group.candidates?.length || 0), 0))
  const active = Number(summary.active ?? total)
  const stopped = Number(summary.stopped ?? 0)
  const bonderCount = Number(summary.bonder_count ?? groups.find(group => group.key === 'bonder')?.candidates?.length ?? 0)
  const jobId = data.context?.type === 'job' ? Number(data.context.id) : undefined

  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
    const previous = document.activeElement as HTMLElement | null
    dialogRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onCloseRef.current() }
    window.addEventListener('keydown', onKeyDown)
    return () => { window.removeEventListener('keydown', onKeyDown); previous?.focus() }
  }, [onClose])

  const detachDialog = (anchor?: DragResizeAnchor): boolean => {
    const candidates = (groups || []).flatMap(group => group.candidates || []).slice(0, 200)
    const jobId = data.context?.type === 'job' ? Number(data.context.id) : 0
    if (candidates.length === 0 && !jobId) return false // 无可弹出内容
    const payload: Record<string, unknown> = { title: data.title || '候选名单' }
    if (candidates.length) payload.candidates = candidates
    if (jobId) payload.url = `/asa-app#job=${encodeURIComponent(String(jobId))}`
    const pos = anchor ?? (() => {
      const el = dialogRef.current
      const rect = el?.getBoundingClientRect()
      return rect ? { x: rect.left, y: rect.top, edge: 'center' } : undefined
    })()
    if (pos) payload.anchor = pos
    if (nativeBridge('openDetachedDialog', payload)) {
      onClose()
      return true
    }
    return false
  }

  // 统一拖动/缩放：header 拖动，右下角 .overlay-resize-handle 缩放；拖出边界弹出独立窗口。
  const detachDialogRef = useRef(detachDialog)
  detachDialogRef.current = detachDialog
  useEffect(() => {
    const el = dialogRef.current
    if (!el) return
    const header = el.querySelector<HTMLElement>('.candidate-dialog-head')
    const cleanupDrag = attachDialogDrag(el, {
      header,
      detach: anchor => detachDialogRef.current(anchor),
      clampPadding: 24,
    })
    const cleanupResize = attachDialogResize(el, {
      minWidth: MIN_DIALOG_W,
      minHeight: MIN_DIALOG_H,
    })
    return () => {
      cleanupDrag()
      cleanupResize()
    }
  }, [])

  return (
    <div className="candidate-dialog-float" role="presentation">
      <section
        ref={dialogRef}
        className="candidate-dialog"
        role="dialog"
        aria-modal="false"
        aria-label={data.title}
        tabIndex={-1}
      >
        <header
          className="candidate-dialog-head"
          style={{ cursor: 'grab', touchAction: 'none' }}
          title="按住拖动；右下角可缩放"
        >
          <span className="candidate-dialog-icon"><Users size={18} /></span>
          <div>
            <h3>{data.title}</h3>
            <small>共 {total} 人{bonderCount > 0 ? ` · 固晶/共晶/键合背景 ${bonderCount} 人` : ''} · 可推进 {active} 人 · 已停止 {stopped} 人</small>
          </div>
          {onRefresh && <button className="icon-btn candidate-dialog-refresh" aria-label="刷新名单" title="重新按库内最新状态生成名单" disabled={refreshing} onClick={onRefresh}>{refreshing ? <LoaderCircle className="spin" size={16}/> : <RefreshCw size={16}/>}</button>}
          <button className="icon-btn candidate-dialog-detach" aria-label="弹出为独立窗口" title="弹出为独立窗口（可拖出屏幕）" onClick={() => detachDialog()}><ExternalLink size={16} /></button>
          <button className="icon-btn" aria-label="关闭名单" title="关闭 (Esc)" onClick={onClose}><X size={16} /></button>
        </header>
        <div className="candidate-dialog-body">
          {groups.map(group => {
            const candidates = group.candidates || []
            if (!candidates.length) return null
            return (
              <div className={`candidate-dialog-group ${group.priority ? 'priority' : ''}`} key={group.key}>
                <h4>{group.priority ? '⭐ ' : ''}{group.label}<em>{candidates.length} 人</em></h4>
                <ul>
                  {candidates.map(candidate => (
                    <li key={candidate.id}>
                      <button
                        className="candidate-dialog-row"
                        onClick={() => onOpenCandidate(candidate.id)}
                        title={`打开人选详情：${candidate.name}`}
                      >
                        <span className="candidate-list-row-main">
                          <b>{candidate.name}</b>
                          <small>{[candidate.company, candidate.title].filter(Boolean).join(' · ')}</small>
                        </span>
                        <em className={`candidate-list-stage tone-${candidateStageTone(candidate.stage)}`}>{candidate.stage || '—'}</em>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )
          })}
          {!groups.some(group => (group.candidates || []).length) && <p className="candidate-dialog-empty">当前候选池为空。</p>}
        </div>
        {jobId !== undefined && onOpenJob && (
          <footer className="candidate-dialog-foot">
            <button className="button" onClick={() => onOpenJob(jobId)}>打开岗位查看完整名单</button>
          </footer>
        )}
        {/* resize handle 由 useEffect 注入到此处，保持 JSX 简洁 */}
      </section>
    </div>
  )
}
