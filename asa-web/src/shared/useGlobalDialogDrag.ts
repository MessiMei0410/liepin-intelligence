import { useEffect } from 'react'

/**
 * 全局弹窗拖动/缩放委托：不改动每个弹窗组件，统一让
 * `.action-dialog` / `.patch-modal` / `.sourcing-result-dialog` 类的模态小窗
 * 支持 header 拖动与右下角 resize。
 * 名单弹窗（.candidate-dialog）与详情面板（.overlay）已有各自的拖动实现，本委托跳过。
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

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement
      if (event.button !== 0) return
      const dialog = target.closest(DIALOG_SELECTOR) as HTMLElement | null
      if (!dialog || isExcluded(dialog)) return
      // 不拦截表单输入、按钮/链接点击；保持对话框内可正常输入。
      // label 包裹的控件点击时 target 是 label，需一并排除，否则 setPointerCapture 会抢走焦点。
      if (target.closest('input, textarea, select, button, a, label')) return

      const rect = dialog.getBoundingClientRect()
      const inResize =
        rect.width > 0 &&
        rect.height > 0 &&
        event.clientX >= rect.right - HANDLE_SIZE &&
        event.clientY >= rect.bottom - HANDLE_SIZE

      const mode: 'drag' | 'resize' = inResize ? 'resize' : 'drag'
      if (mode === 'drag') {
        const header = dialog.querySelector(HEADER_SELECTOR)
        if (!header?.contains(target)) return
      }

      const backdrop = dialog.parentElement
      if (!backdrop) return

      const startX = event.clientX
      const startY = event.clientY

      if (mode === 'resize') {
        const origW = rect.width
        const origH = rect.height
        // resize 只改宽高，不设 position/placeItems —— 保持 grid/flex 居中或已拖出的原位，
        // 避免弹窗跳动到左上角。
        const onMove = (moveEvent: PointerEvent) => {
          const dx = moveEvent.clientX - startX
          const dy = moveEvent.clientY - startY
          dialog.style.width = `${Math.max(MIN_W, origW + dx)}px`
          dialog.style.height = `${Math.max(MIN_H, origH + dy)}px`
          dialog.style.maxWidth = 'none'
          dialog.style.maxHeight = 'none'
        }
        const finish = () => {
          window.removeEventListener('pointermove', onMove)
          window.removeEventListener('pointerup', finish)
          window.removeEventListener('pointercancel', finish)
        }
        window.addEventListener('pointermove', onMove)
        window.addEventListener('pointerup', finish)
        window.addEventListener('pointercancel', finish)
      } else {
        const origLeft = rect.left
        const origTop = rect.top
        let dragging = false
        const onMove = (moveEvent: PointerEvent) => {
          const dx = moveEvent.clientX - startX
          const dy = moveEvent.clientY - startY
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
          if ((backdrop as HTMLElement).style) (backdrop as HTMLElement).style.placeItems = 'start'
        }
        const finish = () => {
          window.removeEventListener('pointermove', onMove)
          window.removeEventListener('pointerup', finish)
          window.removeEventListener('pointercancel', finish)
        }
        window.addEventListener('pointermove', onMove)
        window.addEventListener('pointerup', finish)
        window.addEventListener('pointercancel', finish)
      }

      try { dialog.setPointerCapture?.(event.pointerId) } catch { /* jsdom/旧浏览器无此 API */ }
    }

    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [])
}
