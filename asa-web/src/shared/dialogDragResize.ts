/**
 * 弹窗拖动/缩放统一模块：所有可拖拽/可缩放弹窗共享同一套实现。
 * - attachDialogResize：注入统一 class 为 .overlay-resize-handle 的右下角手柄，负责缩放。
 * - attachDialogDrag：从 header 按住拖动整个弹窗，可选拖出边界后 detach 为独立窗口。
 * 两个函数都直接写 DOM style（不经 React state），避免大组件树每帧重渲染。
 */

export interface DragResizeAnchor {
  x: number
  y: number
  edge: 'left' | 'right' | 'top' | 'bottom' | 'center'
}

export interface ResizeOptions {
  minWidth?: number
  minHeight?: number
  handleSize?: number
}

export interface DragOptions {
  header?: HTMLElement | null
  /** 返回 true 表示本次 pointerdown 不应开始拖动（如落在输入框/按钮/模态打开时）。 */
  shouldIgnore?: (target: EventTarget | null) => boolean
  /** 拖出视口边界时调用；返回 true 表示已接管（如已弹出独立窗口），不再继续拖动。 */
  detach?: (anchor: DragResizeAnchor) => boolean
  /** 视口边缘保留的可见像素，默认 48。 */
  clampPadding?: number
  /** 实际移动的元素；默认为传入的 dialog 本身。 */
  moveElement?: HTMLElement
}

const DEFAULT_MIN_W = 320
const DEFAULT_MIN_H = 240
const DEFAULT_HANDLE_SIZE = 28
const DEFAULT_CLAMP_PADDING = 48

export function attachDialogResize(
  element: HTMLElement,
  { minWidth = DEFAULT_MIN_W, minHeight = DEFAULT_MIN_H, handleSize = DEFAULT_HANDLE_SIZE }: ResizeOptions = {},
): () => void {
  const handle = document.createElement('div')
  handle.className = 'overlay-resize-handle'
  handle.setAttribute('aria-hidden', 'true')
  handle.title = '拖动缩放'
  handle.style.width = `${handleSize}px`
  handle.style.height = `${handleSize}px`
  element.appendChild(handle)

  const state = { startX: 0, startY: 0, origW: 0, origH: 0, active: false }

  const onPointerDown = (event: PointerEvent) => {
    if (event.button !== 0) return
    const rect = element.getBoundingClientRect()
    state.startX = event.clientX
    state.startY = event.clientY
    state.origW = rect.width
    state.origH = rect.height
    state.active = true
    try { handle.setPointerCapture?.(event.pointerId) } catch { /* jsdom/旧浏览器无此 API */ }
    event.stopPropagation() // 不冒泡到 header 拖动
    event.preventDefault()
  }
  const onPointerMove = (event: PointerEvent) => {
    if (!state.active) return
    const dx = event.clientX - state.startX
    const dy = event.clientY - state.startY
    element.style.width = `${Math.max(minWidth, state.origW + dx)}px`
    element.style.height = `${Math.max(minHeight, state.origH + dy)}px`
    element.style.maxWidth = 'none'
    element.style.maxHeight = 'none'
  }
  const onPointerUp = () => { state.active = false }

  handle.addEventListener('pointerdown', onPointerDown)
  handle.addEventListener('pointermove', onPointerMove)
  handle.addEventListener('pointerup', onPointerUp)
  handle.addEventListener('pointercancel', onPointerUp)

  return () => {
    handle.removeEventListener('pointerdown', onPointerDown)
    handle.removeEventListener('pointermove', onPointerMove)
    handle.removeEventListener('pointerup', onPointerUp)
    handle.removeEventListener('pointercancel', onPointerUp)
    handle.remove()
  }
}

export function attachDialogDrag(
  dialog: HTMLElement,
  {
    header,
    shouldIgnore,
    detach,
    clampPadding = DEFAULT_CLAMP_PADDING,
    moveElement,
  }: DragOptions = {},
): () => void {
  const target = moveElement || dialog
  const state = { startX: 0, startY: 0, origX: 0, origY: 0, active: false }

  const onPointerDown = (event: PointerEvent) => {
    if (event.button !== 0) return
    if (shouldIgnore?.(event.target)) return
    // 不拦截按钮/链接点击
    const el = event.target as HTMLElement | null
    if (el?.closest?.('button, a')) return
    if (header && !header.contains(event.target as Node)) return
    const rect = target.getBoundingClientRect()
    state.startX = event.clientX
    state.startY = event.clientY
    state.origX = rect.left
    state.origY = rect.top
    state.active = true
    try { dialog.setPointerCapture?.(event.pointerId) } catch { /* jsdom/旧浏览器无此 API */ }
    event.preventDefault()
  }
  const onPointerMove = (event: PointerEvent) => {
    if (!state.active) return
    const dx = event.clientX - state.startX
    const dy = event.clientY - state.startY
    const nextX = state.origX + dx
    const nextY = state.origY + dy
    const panelW = target.offsetWidth
    const panelH = target.offsetHeight
    const vw = window.innerWidth
    const vh = window.innerHeight
    // 拖出任意边界外侧 → 弹出为独立窗口
    if (detach) {
      const outLeft = nextX < -panelW + clampPadding
      const outTop = nextY < -panelH + clampPadding
      const outRight = nextX > vw - clampPadding
      const outBottom = nextY > vh - clampPadding
      if (outLeft || outTop || outRight || outBottom) {
        const anchor: DragResizeAnchor = {
          x: nextX,
          y: nextY,
          edge: outRight ? 'right' : outLeft ? 'left' : outTop ? 'top' : 'bottom',
        }
        if (detach(anchor)) {
          state.active = false
          return
        }
        // detach 不可用：退回钳制移动，不卡住
      }
    }
    target.style.position = 'fixed'
    target.style.left = `${Math.min(vw - clampPadding, Math.max(-panelW + clampPadding, nextX))}px`
    target.style.top = `${Math.min(vh - clampPadding, Math.max(-panelH + clampPadding, nextY))}px`
    target.style.transform = 'none'
    target.style.margin = '0'
  }
  const onPointerUp = () => { state.active = false }

  dialog.addEventListener('pointerdown', onPointerDown)
  dialog.addEventListener('pointermove', onPointerMove)
  dialog.addEventListener('pointerup', onPointerUp)
  dialog.addEventListener('pointercancel', onPointerUp)

  return () => {
    dialog.removeEventListener('pointerdown', onPointerDown)
    dialog.removeEventListener('pointermove', onPointerMove)
    dialog.removeEventListener('pointerup', onPointerUp)
    dialog.removeEventListener('pointercancel', onPointerUp)
  }
}
