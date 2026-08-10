import { useRef } from 'react'

/**
 * 详情面板可拖动 Hook：header 按住拖动整个 .overlay（模态面板）移动，
 * 拖动时自动钳制在视口内（保留 48px 可见，防止拖丢），松手后保持位置。
 * 拖动中直接写 DOM style（不经 React state），避免大组件树每帧重渲染。
 * 返回 { overlayRef, panelRef, dragProps } 绑定到 overlay 容器与 header。
 */
export function useDraggableOverlay() {
  const overlayRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const drag = useRef<{ startX: number; startY: number; origLeft: number; origTop: number } | null>(null)

  const applyPosition = (left: number, top: number) => {
    const el = overlayRef.current
    if (!el) return
    el.style.left = `${Math.round(left)}px`
    el.style.top = `${Math.round(top)}px`
  }

  const onPointerDown = (event: React.PointerEvent) => {
    if (event.button !== 0) return
    if ((event.target as HTMLElement).closest?.('button, a')) return // 不拦截 header 内按钮/链接点击
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
