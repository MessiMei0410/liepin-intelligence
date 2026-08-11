import { useEffect, useRef } from 'react'
import { attachDialogResize } from './dialogDragResize'

/**
 * 详情面板可拖动/缩放 Hook：header 按住拖动整个 .overlay（模态面板）移动，
 * 右下角注入统一 .overlay-resize-handle 手柄（28px 热区）按住可缩放面板自身。
 * 拖动/缩放时直接写 DOM style（不经 React state），避免大组件树每帧重渲染。
 * 返回 { overlayRef, panelRef, dragProps } 绑定到 overlay 容器与 header。
 *
 * ⚠️ resize 不挂在 header 事件上（header 在顶部永远点不到右下角）：
 * 由共享模块 attachDialogResize 注入独立手柄并绑定 Pointer Events。
 */
export function useDraggableOverlay() {
  const overlayRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const drag = useRef<{ startX: number; startY: number; origLeft: number; origTop: number } | null>(null)

  const HANDLE_SIZE = 28
  const MIN_W = 320
  const MIN_H = 240

  // 注入右下角 resize 手柄：与所有弹窗共用同一实现。
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    return attachDialogResize(panel, { minWidth: MIN_W, minHeight: MIN_H, handleSize: HANDLE_SIZE })
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
    // label 包裹的控件点击时 target 是 label，需一并排除，否则 setPointerCapture 会抢走焦点。
    if (el.closest?.('input, textarea, select, label')) return true
    // 不拦截按钮/链接点击。
    if (el.closest?.('button, a')) return true
    // 不拦截 resize 手柄（手柄有独立事件）。
    if (el.closest?.('.overlay-resize-handle')) return true
    // 当模态对话框打开时，面板 header 不应再抢 pointer 事件，
    // 避免拖动导致模态框跟着移动、输入被打断。
    // 注意：.candidate-dialog-float 是非模态名单弹窗，不应阻止面板拖动。
    if (document.querySelector('.action-dialog-backdrop, .patch-modal-backdrop')) return true
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
