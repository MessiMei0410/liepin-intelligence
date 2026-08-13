/**
 * 弹窗拖动/缩放统一模块：所有可拖拽/可缩放弹窗共享同一套实现。
 * - attachDialogResize：注入统一 class 为 .overlay-resize-handle 的右下角手柄，负责缩放。
 * - attachDialogDrag：从 header 按住拖动整个弹窗，可选拖出边界后 detach 为独立窗口。
 * 两个函数都直接写 DOM style（不经 React state），避免大组件树每帧重渲染。
 *
 * WKWebView 兼容：ASA 桌面端由 WKWebView 承载页面，Pointer Events 的
 * setPointerCapture / pointermove 派发存在已知兼容性问题；且按规范对 pointerdown
 * 调用 preventDefault 会抑制兼容 mouse 事件，把兜底通道一并掐断（Chrome 下 pointer
 * 正常所以无感，WKWebView 下就表现为“整个窗口拖不动”）。
 * 因此这里不 preventDefault、不 setPointerCapture，改为在 document 上同时挂
 * pointer/mouse 双通道：先收到移动事件的一侧生效，另一侧忽略；up/cancel/blur
 * 幂等收尾。Chromium 下同样工作正常。
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
  /** 返回 true 表示本次按下不应开始拖动（如落在输入框/按钮/模态打开时）。 */
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
/** 光标贴近视口边缘多少像素内视为“要拖出窗口”。 */
const DETACH_EDGE_PX = 16
/** 光标需顶住边缘停留多久才触发脱出，避免正常挪动窗口时误弹。 */
const DETACH_DWELL_MS = 350

type DragChannel = 'pointer' | 'mouse'

interface DragTrackState {
  active: boolean
  channel: DragChannel | null
}

/** 拖动期间在 document 上挂 pointer/mouse 双通道监听；返回清理函数。 */
function trackDragMoves(
  state: DragTrackState,
  onMove: (clientX: number, clientY: number) => void,
  onEnd: () => void,
): () => void {
  const handlePointerMove = (event: PointerEvent) => {
    if (!state.active || state.channel === 'mouse') return
    state.channel = 'pointer'
    onMove(event.clientX, event.clientY)
  }
  const handleMouseMove = (event: MouseEvent) => {
    if (!state.active || state.channel === 'pointer') return
    state.channel = 'mouse'
    onMove(event.clientX, event.clientY)
  }
  const end = () => {
    if (!state.active) return
    state.active = false
    state.channel = null
    onEnd()
  }
  document.addEventListener('pointermove', handlePointerMove)
  document.addEventListener('mousemove', handleMouseMove)
  document.addEventListener('pointerup', end)
  document.addEventListener('pointercancel', end)
  document.addEventListener('mouseup', end)
  window.addEventListener('blur', end)
  return () => {
    document.removeEventListener('pointermove', handlePointerMove)
    document.removeEventListener('mousemove', handleMouseMove)
    document.removeEventListener('pointerup', end)
    document.removeEventListener('pointercancel', end)
    document.removeEventListener('mouseup', end)
    window.removeEventListener('blur', end)
  }
}

/** 拖动期间禁用文本选择，结束后恢复原值。 */
function lockSelection(): () => void {
  const body = document.body
  if (!body) return () => {}
  const previous = body.style.userSelect
  body.style.userSelect = 'none'
  return () => {
    body.style.userSelect = previous
  }
}

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

  const state: DragTrackState = { active: false, channel: null }
  const start = { x: 0, y: 0, w: 0, h: 0 }
  let unlockSelection: (() => void) | null = null

  const finish = () => {
    state.active = false
    state.channel = null
    unlockSelection?.()
    unlockSelection = null
  }

  const cleanupTrack = trackDragMoves(
    state,
    (clientX, clientY) => {
      const dx = clientX - start.x
      const dy = clientY - start.y
      element.style.width = `${Math.max(minWidth, start.w + dx)}px`
      element.style.height = `${Math.max(minHeight, start.h + dy)}px`
      element.style.maxWidth = 'none'
      element.style.maxHeight = 'none'
    },
    finish,
  )

  const onDown = (event: MouseEvent | PointerEvent) => {
    if (state.active) return
    if (event.button !== 0) return
    const rect = element.getBoundingClientRect()
    start.x = event.clientX
    start.y = event.clientY
    start.w = rect.width
    start.h = rect.height
    state.active = true
    state.channel = null
    unlockSelection = lockSelection()
    event.stopPropagation() // 不冒泡到 header 拖动
  }

  handle.addEventListener('pointerdown', onDown)
  handle.addEventListener('mousedown', onDown)

  return () => {
    handle.removeEventListener('pointerdown', onDown)
    handle.removeEventListener('mousedown', onDown)
    cleanupTrack()
    finish()
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
  const state: DragTrackState = { active: false, channel: null }
  const start = { x: 0, y: 0, panelLeft: 0, panelTop: 0, padLeft: 0, padTop: 0, headerH: 0 }
  let unlockSelection: (() => void) | null = null
  // 拖出窗口（detach）手势状态：WKWebView 在光标越过窗口边界后不再派发移动事件，
  // “面板几乎完全拖出视口”的条件在桌面端永远达不到；因此改为光标顶住视口边缘
  // 停留 DETACH_DWELL_MS 触发脱出。停留计时避免正常挪动窗口时误弹。
  let lastX = 0
  let lastY = 0
  let detachEdge: DragResizeAnchor['edge'] | null = null
  let detachDeclined = false
  let dwellTimer: ReturnType<typeof setTimeout> | null = null

  const clearDwell = () => {
    if (dwellTimer !== null) {
      clearTimeout(dwellTimer)
      dwellTimer = null
    }
  }

  const finish = () => {
    state.active = false
    state.channel = null
    clearDwell()
    detachEdge = null
    detachDeclined = false
    unlockSelection?.()
    unlockSelection = null
  }

  const cleanupTrack = trackDragMoves(
    state,
    (clientX, clientY) => {
      const dx = clientX - start.x
      const dy = clientY - start.y
      // 以“面板跟随光标”为目标计算目标位移：overlay 的 padding / place-items
      // 布局在拖动后可能变化，直接按 target 原点位移会导致面板滑位。
      const nextPanelLeft = start.panelLeft + dx
      const nextPanelTop = start.panelTop + dy
      const panelW = dialog.offsetWidth
      const panelH = dialog.offsetHeight
      const vw = window.innerWidth
      const vh = window.innerHeight
      lastX = clientX
      lastY = clientY
      // 光标顶住视口边缘并停留片刻 → 弹出为独立窗口
      if (detach) {
        const near: DragResizeAnchor['edge'] | null =
          clientX >= vw - DETACH_EDGE_PX ? 'right'
            : clientX <= DETACH_EDGE_PX ? 'left'
              : clientY <= DETACH_EDGE_PX ? 'top'
                : clientY >= vh - DETACH_EDGE_PX ? 'bottom'
                  : null
        if (near !== detachEdge) {
          detachEdge = near
          detachDeclined = false
          clearDwell()
        }
        if (near && !detachDeclined && dwellTimer === null) {
          dwellTimer = setTimeout(() => {
            dwellTimer = null
            if (!state.active || !detach) return
            const anchor: DragResizeAnchor = { x: lastX, y: lastY, edge: detachEdge ?? 'center' }
            if (detach(anchor)) {
              finish()
            } else {
              // detach 不可用（如无原生桥）：本次贴边不再重复尝试，退回钳制移动
              detachDeclined = true
            }
          }, DETACH_DWELL_MS)
        }
      }
      const clampedLeft = Math.min(vw - clampPadding, Math.max(-panelW + clampPadding, nextPanelLeft))
      // 头部是唯一拖动把手：垂直方向钳制到“头部始终完整可见”，
      // 避免把窗口拖出屏幕后头部够不着、无法拖回。
      const headerH = start.headerH
      const topMin = headerH > 0 ? 0 : -panelH + clampPadding
      const topMax = vh - Math.max(clampPadding, headerH)
      const clampedTop = Math.min(topMax, Math.max(topMin, nextPanelTop))
      target.style.position = 'fixed'
      target.style.left = `${clampedLeft - start.padLeft}px`
      target.style.top = `${clampedTop - start.padTop}px`
      target.style.transform = 'none'
      target.style.margin = '0'
    },
    finish,
  )

  const onDown = (event: MouseEvent | PointerEvent) => {
    if (state.active) return
    if (event.button !== 0) return
    if (shouldIgnore?.(event.target)) return
    // 不拦截按钮/链接点击
    const el = event.target as HTMLElement | null
    if (el?.closest?.('button, a')) return
    if (header && !header.contains(event.target as Node)) return
    const panelRect = dialog.getBoundingClientRect()
    const targetStyle = getComputedStyle(target)
    start.x = event.clientX
    start.y = event.clientY
    start.panelLeft = panelRect.left
    start.panelTop = panelRect.top
    start.padLeft = parseFloat(targetStyle.paddingLeft) || 0
    start.padTop = parseFloat(targetStyle.paddingTop) || 0
    start.headerH = header?.offsetHeight ?? 0
    // 锁定面板尺寸：overlay 拖动后 grid 布局从居中切换为 start，
    // 若不做锁定，面板会随 shrink-to-fit 塌缩变形。
    dialog.style.width = `${panelRect.width}px`
    dialog.style.height = `${panelRect.height}px`
    state.active = true
    state.channel = null
    unlockSelection = lockSelection()
  }

  dialog.addEventListener('pointerdown', onDown)
  dialog.addEventListener('mousedown', onDown)

  return () => {
    dialog.removeEventListener('pointerdown', onDown)
    dialog.removeEventListener('mousedown', onDown)
    cleanupTrack()
    finish()
  }
}
