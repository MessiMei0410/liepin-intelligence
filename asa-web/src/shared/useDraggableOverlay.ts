import { useEffect, useRef } from 'react'

/**
 * 详情面板可拖动/缩放 Hook：header 按住拖动整个 .overlay（模态面板）移动，
 * 右下角注入独立 resize 手柄（20px 区域）按住可缩放面板自身（写 panel.style.width/height）。
 * 拖动/缩放时直接写 DOM style（不经 React state），避免大组件树每帧重渲染。
 * 返回 { overlayRef, panelRef, dragProps } 绑定到 overlay 容器与 header。
 *
 * ⚠️ resize 不挂在 header 事件上（header 在顶部永远点不到右下角）：
 * 由 hook 在 panel 右下角注入 `.overlay-resize-handle` 元素并单独绑定 Pointer Events。
 */
export function useDraggableOverlay() {
  const overlayRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const drag = useRef<{ startX: number; startY: number; origLeft: number; origTop: number } | null>(null)
  const resize = useRef<{ startX: number; startY: number; origW: number; origH: number } | null>(null)

  const HANDLE_SIZE = 20
  const MIN_W = 280
  const MIN_H = 200

  // 注入右下角 resize 手柄：独立元素 + 独立事件，避免与 header 拖动相互干扰。
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const handle = document.createElement('div')
    handle.className = 'overlay-resize-handle'
    handle.setAttribute('aria-hidden', 'true')
    handle.style.cssText = `position:absolute;right:0;bottom:0;width:${HANDLE_SIZE}px;height:${HANDLE_SIZE}px;cursor:nwse-resize;touch-action:none;z-index:20;`
    panel.appendChild(handle)

    const onHandleDown = (event: PointerEvent) => {
      if (event.button !== 0) return
      const rect = panel.getBoundingClientRect()
      resize.current = { startX: event.clientX, startY: event.clientY, origW: rect.width, origH: rect.height }
      try { handle.setPointerCapture?.(event.pointerId) } catch { /* jsdom/旧浏览器无此 API */ }
      event.stopPropagation() // 不冒泡到 header 拖动
      event.preventDefault()
    }
    const onHandleMove = (event: PointerEvent) => {
      const state = resize.current
      if (!state) return
      const dx = event.clientX - state.startX
      const dy = event.clientY - state.startY
      panel.style.width = `${Math.max(MIN_W, state.origW + dx)}px`
      panel.style.height = `${Math.max(MIN_H, state.origH + dy)}px`
      panel.style.maxWidth = 'none'
      panel.style.maxHeight = 'none'
    }
    const onHandleUp = () => { resize.current = null }
    handle.addEventListener('pointerdown', onHandleDown)
    handle.addEventListener('pointermove', onHandleMove)
    handle.addEventListener('pointerup', onHandleUp)
    handle.addEventListener('pointercancel', onHandleUp)
    return () => {
      handle.removeEventListener('pointerdown', onHandleDown)
      handle.removeEventListener('pointermove', onHandleMove)
      handle.removeEventListener('pointerup', onHandleUp)
      handle.removeEventListener('pointercancel', onHandleUp)
      handle.remove()
    }
  }, [])

  const applyPosition = (left: number, top: number) => {
    const el = overlayRef.current
    if (!el) return
    el.style.left = `${Math.round(left)}px`
    el.style.top = `${Math.round(top)}px`
  }

  const shouldIgnoreDragStart = (target: EventTarget | null): boolean => {
    const el = target as HTMLElement | null
    if (!el) return true
    // 不拦截表单输入：textarea/select/input 需要正常获得焦点。
    if (el.closest?.('input, textarea, select')) return true
    // 不拦截按钮/链接点击。
    if (el.closest?.('button, a')) return true
    // 不拦截 resize 手柄（手柄有独立事件）。
    if (el.closest?.('.overlay-resize-handle')) return true
    // 当有模态对话框打开时（action-dialog/patch-modal/candidate-dialog），
    // 面板 header 不应再抢 pointer 事件，避免拖动导致模态框跟着移动、输入被打断。
    if (document.querySelector('.action-dialog-backdrop, .patch-modal-backdrop, .candidate-dialog-float')) return true
    return false
  }

  const onPointerDown = (event: React.PointerEvent) => {
    if (event.button !== 0) return
    if (shouldIgnoreDragStart(event.target)) return
    const el = overlayRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    drag.current = { startX: event.clientX, startY: event.clientY, origLeft: rect.left, origTop: rect.top }
    try { event.currentTarget.setPointerCapture?.(event.pointerId) } catch { /* jsdom/旧浏览器无此 API */ }
  }
  const onPointerMove = (event: React.PointerEvent) => {
    const state = drag.current
    if (!state) return
    const dx = event.clientX - state.startX
    const dy = event.clientY - state.startY
    const nextX = state.origLeft + dx
    const nextY = state.origTop + dy
    // 视口钳制：至少保留 48px 在可见区域，避免拖丢。
    const panelWidth = panelRef.current?.offsetWidth || 0
    const panelHeight = panelRef.current?.offsetHeight || 0
    const minX = -panelWidth + 48
    const minY = -panelHeight + 48
    const maxX = window.innerWidth - 48
    const maxY = window.innerHeight - 48
    applyPosition(Math.min(maxX, Math.max(minX, nextX)), Math.min(maxY, Math.max(minY, nextY)))
  }
  const onPointerUp = () => { drag.current = null }
  const onPointerCancel = () => { drag.current = null }

  return {
    overlayRef,
    panelRef,
    dragProps: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel },
  }
}
