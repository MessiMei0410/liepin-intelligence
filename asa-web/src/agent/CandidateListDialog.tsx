import { useEffect, useRef, useState } from 'react'
import { ExternalLink, Users, X } from 'lucide-react'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

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
}: {
  data: CandidateListCardData
  onOpenCandidate: (jobCandidateId: number) => void
  onOpenJob?: (jobId: number) => void
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLElement>(null)
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null)
  const dragState = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null)
  const resizeState = useRef<{ startX: number; startY: number; origW: number; origH: number } | null>(null)
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

  const ensurePositioned = (rect: DOMRect) => {
    setPosition(prev => {
      if (prev) return prev
      return { x: rect.left, y: rect.top }
    })
  }

  // 拖拽弹窗：header 区域拖动。
  // 缩放由独立的 .candidate-dialog-resize-handle 元素处理，避免与 body 滚动条/按钮冲突。
  const onPointerDown = (event: React.PointerEvent) => {
    if (event.button !== 0) return
    if ((event.target as HTMLElement).closest?.('button, a')) return // 不拦截按钮/链接点击
    const el = dialogRef.current
    if (!el) return
    // 只允许从 header 区域开始拖动（body 是滚动列表，不能误触）。
    const head = el.querySelector('.candidate-dialog-head')
    if (!head?.contains(event.target as Node)) return
    const rect = el.getBoundingClientRect()
    dragState.current = { startX: event.clientX, startY: event.clientY, origX: rect.left, origY: rect.top }
    // move/up 挂到 window，避免 setPointerCapture 的兼容问题
    const onMove = (moveEvent: PointerEvent) => handleDragMove(moveEvent.clientX, moveEvent.clientY)
    const onUp = () => {
      dragState.current = null
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  const handleDragMove = (clientX: number, clientY: number) => {
    const state = dragState.current
    if (!state) return
    const el = dialogRef.current
    if (!el) return
    const dx = clientX - state.startX
    const dy = clientY - state.startY
    const panelW = el.offsetWidth
    const panelH = el.offsetHeight
    const vw = window.innerWidth, vh = window.innerHeight
    const nextX = state.origX + dx
    const nextY = state.origY + dy
    // 拖出任意边界外侧 → 弹出为独立窗口，且定位到拖出方向
    const outLeft = nextX < -panelW + 24
    const outTop = nextY < -panelH + 24
    const outRight = nextX > vw - 24
    const outBottom = nextY > vh - 24
    if (outLeft || outTop || outRight || outBottom) {
      const anchor = { x: nextX, y: nextY, edge: outRight ? 'right' : outLeft ? 'left' : outTop ? 'top' : 'bottom' }
      if (detachDialog(anchor)) {
        dragState.current = null
        return
      }
      // native 不可用（浏览器预览等）：不卡住，退回钳制移动
    }
    setPosition({
      x: Math.min(vw - 48, Math.max(-panelW + 48, nextX)),
      y: Math.min(vh - 48, Math.max(-panelH + 48, nextY)),
    })
  }

  // 独立 resize 手柄：挂载到 window 的 move/up，避免 setPointerCapture 在 WKWebView 下不稳定。
  useEffect(() => {
    const el = dialogRef.current
    if (!el) return
    const handle = document.createElement('div')
    handle.className = 'candidate-dialog-resize-handle'
    handle.setAttribute('aria-hidden', 'true')
    handle.title = '拖动缩放'
    el.appendChild(handle)

    const onDown = (event: PointerEvent) => {
      if (event.button !== 0) return
      const rect = el.getBoundingClientRect()
      resizeState.current = { startX: event.clientX, startY: event.clientY, origW: rect.width, origH: rect.height }
      ensurePositioned(rect)
      el.style.position = 'fixed'
      el.style.left = `${rect.left}px`
      el.style.top = `${rect.top}px`
      el.style.transform = 'none'
      el.style.margin = '0'
      el.style.maxWidth = 'none'
      el.style.maxHeight = 'none'
      event.stopPropagation()
      event.preventDefault()
    }
    const onMove = (event: PointerEvent) => {
      const state = resizeState.current
      if (!state) return
      const dx = event.clientX - state.startX
      const dy = event.clientY - state.startY
      el.style.width = `${Math.max(MIN_DIALOG_W, state.origW + dx)}px`
      el.style.height = `${Math.max(MIN_DIALOG_H, state.origH + dy)}px`
    }
    const onUp = () => { resizeState.current = null }
    handle.addEventListener('pointerdown', onDown)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    return () => {
      handle.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      handle.remove()
    }
  }, [])

  const detachDialog = (anchor?: { x: number; y: number; edge: string }): boolean => {
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

  return (
    <div className="candidate-dialog-float" role="presentation">
      <section
        ref={dialogRef}
        className="candidate-dialog"
        role="dialog"
        aria-modal="false"
        aria-label={data.title}
        tabIndex={-1}
        style={position ? { left: position.x, top: position.y, position: 'fixed', margin: 0 } : undefined}
        onPointerDown={onPointerDown}
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
                        <em className="candidate-list-stage">{candidate.stage || '—'}</em>
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
