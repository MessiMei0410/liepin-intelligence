import { useEffect } from 'react'

/**
 * 全局弹窗拖动委托：不改动每个弹窗组件，统一让
 * `.action-dialog-backdrop > .action-dialog` 类的模态小窗可拖动（header 区域），
 * 拖到视口上/左边界外侧时触发 openDetachedDialog（若可用）。
 * 名单弹窗（.candidate-dialog）与详情面板（.overlay）已有各自的拖动实现，本委托跳过。
 */
export function useGlobalDialogDrag() {
  useEffect(() => {
    const isExcluded = (el: Element | null) =>
      !!el?.closest?.('.candidate-dialog, .overlay, .sourcing-result-dialog-backdrop')

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement
      if (event.button !== 0) return
      const dialog = target.closest('.action-dialog, .patch-modal') as HTMLElement | null
      if (!dialog || isExcluded(dialog)) return
      if (target.closest('button, a')) return // 不拦截按钮/链接

      // 只从 header 区域开始拖动（action-dialog 的 header 是弹窗顶部条）
      const header = dialog.querySelector('header')
      if (!header?.contains(target)) return

      const backdrop = dialog.parentElement
      if (!backdrop) return
      const startX = event.clientX
      const startY = event.clientY
      const dialogRect = dialog.getBoundingClientRect()
      const origLeft = dialogRect.left
      const origTop = dialogRect.top
      let dragging = false
      const DRAG_THRESHOLD = 4

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
        // 只在视口内钳制移动：action-dialog 无结构化上下文，不弹独立窗口（避免空白 detach 丢内容）
        dialog.style.left = `${Math.min(vw - 48, Math.max(-panelW + 48, nextX))}px`
        dialog.style.top = `${Math.min(vh - 48, Math.max(-panelH + 48, nextY))}px`
        dialog.style.transform = 'none'
        dialog.style.margin = '0'
        backdrop.style.placeItems = 'start'
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

    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [])
}
