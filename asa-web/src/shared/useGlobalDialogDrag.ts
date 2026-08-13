import { useEffect } from 'react'

/**
 * 全局弹窗拖动/缩放委托：不改动每个弹窗组件，统一让
 * `.action-dialog` / `.patch-modal` / `.sourcing-result-dialog` 类的模态小窗
 * 支持 header 拖动与右下角 resize。
 * 名单弹窗（.candidate-dialog）与详情面板（.overlay）已有各自的拖动实现，本委托跳过。
 *
 * WKWebView 兼容：与 dialogDragResize 一致，不 setPointerCapture，
 * 按下后同时挂 pointer/mouse 双通道监听（先到者生效），up/cancel/blur 幂等收尾。
 */
export function useGlobalDialogDrag() {
  useEffect(() => {
    const DIALOG_SELECTOR = '.action-dialog, .patch-modal, .sourcing-result-dialog'
    const HEADER_SELECTOR = 'header, .patch-modal-head, .modal-head, .sourcing-result-head'
    const isExcluded = (el: Element | null) =>
      !!el?.closest?.('.candidate-dialog, .overlay')

    const MIN_W = 280
    const MIN_H = 200
    const HANDLE_SIZE = 20
    const DRAG_THRESHOLD = 4

    type DragChannel = 'pointer' | 'mouse'
    const trackState: { active: boolean; channel: DragChannel | null } = { active: false, channel: null }
    let mode: 'drag' | 'resize' = 'drag'
    let startX = 0
    let startY = 0
    let origLeft = 0
    let origTop = 0
    let origW = 0
    let origH = 0
    let dialog: HTMLElement | null = null
    let backdrop: HTMLElement | null = null
    let dragging = false
    let unlockSelection: (() => void) | null = null

    const finish = () => {
      trackState.active = false
      trackState.channel = null
      unlockSelection?.()
      unlockSelection = null
      dialog = null
      backdrop = null
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('pointerup', end)
      window.removeEventListener('pointercancel', end)
      window.removeEventListener('mouseup', end)
      window.removeEventListener('blur', end)
    }
    const end = () => {
      if (trackState.active) finish()
    }

    const applyMove = (clientX: number, clientY: number) => {
      if (!trackState.active || !dialog || !backdrop) return
      const dx = clientX - startX
      const dy = clientY - startY
      if (mode === 'resize') {
        // resize 只改宽高，不设 position/placeItems —— 保持 grid/flex 居中或已拖出的原位，
        // 避免弹窗跳动到左上角。
        dialog.style.width = `${Math.max(MIN_W, origW + dx)}px`
        dialog.style.height = `${Math.max(MIN_H, origH + dy)}px`
        dialog.style.maxWidth = 'none'
        dialog.style.maxHeight = 'none'
        return
      }
      if (!dragging && Math.hypot(dx, dy) < DRAG_THRESHOLD) return // 点击/抖动不算拖动
      dragging = true
      const panelW = dialog.offsetWidth
      const panelH = dialog.offsetHeight
      const vw = window.innerWidth
      const vh = window.innerHeight
      const nextX = origLeft + dx
      const nextY = origTop + dy
      dialog.style.left = `${Math.min(vw - 48, Math.max(-panelW + 48, nextX))}px`
      dialog.style.top = `${Math.min(vh - 48, Math.max(-panelH + 48, nextY))}px`
      dialog.style.position = 'fixed'
      dialog.style.transform = 'none'
      dialog.style.margin = '0'
      backdrop.style.placeItems = 'start'
    }

    const handlePointerMove = (event: PointerEvent) => {
      if (!trackState.active || trackState.channel === 'mouse') return
      trackState.channel = 'pointer'
      applyMove(event.clientX, event.clientY)
    }
    const handleMouseMove = (event: MouseEvent) => {
      if (!trackState.active || trackState.channel === 'pointer') return
      trackState.channel = 'mouse'
      applyMove(event.clientX, event.clientY)
    }

    const begin = (event: MouseEvent | PointerEvent) => {
      if (trackState.active) return
      const target = event.target as HTMLElement
      if (event.button !== 0) return
      const el = target.closest(DIALOG_SELECTOR) as HTMLElement | null
      if (!el || isExcluded(el)) return
      // 不拦截表单输入、按钮/链接点击；保持对话框内可正常输入。
      // label 包裹的控件点击时 target 是 label，需一并排除。
      if (target.closest('input, textarea, select, button, a, label')) return

      const rect = el.getBoundingClientRect()
      const inResize =
        rect.width > 0 &&
        rect.height > 0 &&
        event.clientX >= rect.right - HANDLE_SIZE &&
        event.clientY >= rect.bottom - HANDLE_SIZE

      mode = inResize ? 'resize' : 'drag'
      if (mode === 'drag') {
        const header = el.querySelector(HEADER_SELECTOR)
        if (!header?.contains(target)) return
      }

      const backdropEl = el.parentElement
      if (!backdropEl) return

      dialog = el
      backdrop = backdropEl
      startX = event.clientX
      startY = event.clientY
      origLeft = rect.left
      origTop = rect.top
      origW = rect.width
      origH = rect.height
      dragging = false
      trackState.active = true
      trackState.channel = null
      unlockSelection = lockSelection()
      window.addEventListener('pointermove', handlePointerMove)
      window.addEventListener('mousemove', handleMouseMove)
      window.addEventListener('pointerup', end)
      window.addEventListener('pointercancel', end)
      window.addEventListener('mouseup', end)
      window.addEventListener('blur', end)
    }

    const onPointerDown = (event: PointerEvent) => begin(event)
    const onMouseDown = (event: MouseEvent) => begin(event)
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('mousedown', onMouseDown, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('mousedown', onMouseDown, true)
      finish()
    }
  }, [])
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
