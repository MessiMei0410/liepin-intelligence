/**
 * 弹窗组件族：统一 z-index 分层、焦点闭环、拖动/缩放。
 *
 * z-index 分层（styles.css :root 变量）：--z-overlay:50（详情面板）< --z-float:60（非模态浮窗）
 * < --z-modal:80（模态框/底部 sheet）。组件只渲染既有 class 名，样式全部沿用 styles.css，
 * 拖动/缩放由 useGlobalDialogDrag 全局委托（模态框）或 dialogDragResize 模块（浮窗/面板）提供。
 *
 * - DialogModal：模态对话框（.action-dialog-backdrop > .action-dialog），内置 Esc/遮罩关闭与焦点闭环。
 * - DialogFloating：非模态浮窗（.candidate-dialog-float > .candidate-dialog），header 拖动 + 右下角缩放 + 可 detach。
 * - DialogPanel：.overlay 详情面板框架（header 拖动移动 overlay + 右下角缩放 + 可选 Esc）。
 *   全部 .overlay 采用者（CandidatePanel/JobPanel/WorkflowPanel/轻量工作流浮层）已统一迁移到本组件。
 * - DialogSheet：底部 sheet（窄屏/浮窗形态），当前无采用者，组件先行。
 */
import { forwardRef, useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { attachDialogDrag, attachDialogResize, type DragResizeAnchor } from './dialogDragResize'
import { isBareDetached } from './nativeBridge'
import { useDialogFocus } from './useDialogFocus'

const cx = (...parts: Array<string | false | undefined>): string => parts.filter(Boolean).join(' ')

/** Esc 关闭：closeDisabled 时忽略（如提交中）。 */
function useDialogEscape(onClose: () => void, closeDisabled: boolean) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !closeDisabled) onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose, closeDisabled])
}

export interface DialogModalProps {
  onClose: () => void
  /** 标题，渲染为 header 的 h3。 */
  title: ReactNode
  /** aria-labelledby 指向的标题 id。 */
  titleId: string
  /** header 左侧图标（.action-dialog-icon）。 */
  icon?: ReactNode
  /** 标题上方的小字（如「工作流」）。 */
  eyebrow?: ReactNode
  /** 底部按钮区，渲染为 <footer>。 */
  footer?: ReactNode
  /** 叠加在 section 上的 class（如 artifact-dialog）。 */
  className?: string
  /** 叠加在遮罩上的 class。 */
  backdropClassName?: string
  /** 叠加在正文容器上的 class（默认带 action-dialog-body）。 */
  bodyClassName?: string
  /** true 时 role=alertdialog（操作确认类）。 */
  alert?: boolean
  /** 提交中等场景禁用一切关闭途径。 */
  closeDisabled?: boolean
  /** 点击遮罩关闭，默认 true。 */
  closeOnBackdrop?: boolean
  /** 关闭按钮的 aria-label/title，默认「关闭」。 */
  closeLabel?: string
  /** 初始焦点 selector，透传 useDialogFocus。 */
  initialFocus?: string
  children: ReactNode
}

/** 模态对话框：.action-dialog 结构 + Esc/遮罩关闭 + 焦点闭环；拖动/缩放走全局委托。 */
export function DialogModal({
  onClose,
  title,
  titleId,
  icon,
  eyebrow,
  footer,
  className,
  backdropClassName,
  bodyClassName,
  alert = false,
  closeDisabled = false,
  closeOnBackdrop = true,
  closeLabel = '关闭',
  initialFocus,
  children,
}: DialogModalProps) {
  const dialogRef = useDialogFocus<HTMLElement>(true, { initialFocus })
  useDialogEscape(onClose, closeDisabled)

  return (
    <div
      className={cx('action-dialog-backdrop', backdropClassName)}
      role="presentation"
      onClick={() => { if (closeOnBackdrop && !closeDisabled) onClose() }}
    >
      <section
        ref={dialogRef}
        className={cx('action-dialog', className)}
        role={alert ? 'alertdialog' : 'dialog'}
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={event => event.stopPropagation()}
      >
        <header>
          {icon && <span className="action-dialog-icon">{icon}</span>}
          <div>
            {eyebrow && <small>{eyebrow}</small>}
            <h3 id={titleId}>{title}</h3>
          </div>
          <button className="icon-btn" disabled={closeDisabled} onClick={onClose} title={closeLabel} aria-label={closeLabel}><X /></button>
        </header>
        <div className={cx('action-dialog-body', bodyClassName)}>{children}</div>
        {footer && <footer>{footer}</footer>}
      </section>
    </div>
  )
}

export interface DialogFloatingProps {
  onClose: () => void
  /** 标题，渲染为 header 的 h3。 */
  title: ReactNode
  /** 无障碍名（浮窗无 aria-labelledby 惯例，直接用 aria-label）。 */
  ariaLabel: string
  /** header 左侧图标（.candidate-dialog-icon）。 */
  icon?: ReactNode
  /** 标题下方的小字（统计摘要等）。 */
  eyebrow?: ReactNode
  /** header 右侧附加按钮（刷新/detach 等），位于关闭按钮之前。 */
  headerActions?: ReactNode
  /** 底部区，渲染为 <footer class="candidate-dialog-foot">。 */
  footer?: ReactNode
  /** 叠加在 section 上的 class。 */
  className?: string
  /** 拖出视口边界时调用；返回 true 表示已接管（如弹出独立窗口）。 */
  onDetach?: (anchor?: DragResizeAnchor) => boolean
  minWidth?: number
  minHeight?: number
  /** 关闭按钮 aria-label 前缀，默认「关闭」。 */
  closeLabel?: string
  children: ReactNode
}

/** 非模态浮窗：挂载聚焦、Esc 关闭、卸载归还焦点；header 拖动 + 右下角缩放 + 可拖出 detach。 */
export const DialogFloating = forwardRef<HTMLElement, DialogFloatingProps>(function DialogFloating(
  {
    onClose,
    title,
    ariaLabel,
    icon,
    eyebrow,
    headerActions,
    footer,
    className,
    onDetach,
    minWidth = 320,
    minHeight = 240,
    closeLabel = '关闭',
    children,
  },
  forwardedRef,
) {
  const innerRef = useRef<HTMLElement | null>(null)
  const setRefs = (el: HTMLElement | null) => {
    innerRef.current = el
    if (typeof forwardedRef === 'function') forwardedRef(el)
    else if (forwardedRef) forwardedRef.current = el
  }

  const onCloseRef = useRef(onClose)
  useEffect(() => { onCloseRef.current = onClose })
  const onDetachRef = useRef(onDetach)
  useEffect(() => { onDetachRef.current = onDetach })

  // 挂载聚焦自身（tabIndex=-1），Esc 关闭，卸载归还之前焦点。
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    innerRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onCloseRef.current() }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      previous?.focus()
    }
  }, [])

  // 统一拖动/缩放：header 拖动，右下角 .overlay-resize-handle 缩放；拖出边界可 detach。
  // 纯净模式（独立窗口）下页面填满窗口，拖动/缩放交给原生标题栏，不再挂载。
  useEffect(() => {
    const el = innerRef.current
    if (!el || isBareDetached()) return undefined
    const header = el.querySelector<HTMLElement>('header')
    const cleanupDrag = attachDialogDrag(el, {
      header,
      detach: anchor => onDetachRef.current?.(anchor) ?? false,
      clampPadding: 24,
    })
    const cleanupResize = attachDialogResize(el, { minWidth, minHeight })
    return () => {
      cleanupDrag()
      cleanupResize()
    }
    // minWidth/minHeight 变化无需重挂（实际消费方均传常量）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="candidate-dialog-float" role="presentation">
      <section
        ref={setRefs}
        className={cx('candidate-dialog', className)}
        role="dialog"
        aria-modal="false"
        aria-label={ariaLabel}
        tabIndex={-1}
      >
        <header className="candidate-dialog-head" style={{ cursor: 'grab', touchAction: 'none' }} title="按住拖动；右下角可缩放">
          {icon && <span className="candidate-dialog-icon">{icon}</span>}
          <div>
            <h3>{title}</h3>
            {eyebrow && <small>{eyebrow}</small>}
          </div>
          {headerActions}
          <button className="icon-btn" aria-label={closeLabel} title={`${closeLabel} (Esc)`} onClick={onClose}><X size={16} /></button>
        </header>
        <div className="candidate-dialog-body">{children}</div>
        {footer && <footer className="candidate-dialog-foot">{footer}</footer>}
      </section>
    </div>
  )
})

export interface DialogPanelProps {
  /** 面板 section 的 class（如 "detail-panel candidate-panel"）。 */
  panelClassName: string
  /** 提供时把面板声明为模态 dialog。 */
  ariaLabel?: string
  /** Esc 回调：模态框（审批/产物/结果卡）打开时自动忽略，避免误关背后面板。 */
  onEscape?: () => void
  /** 拖出视口边界时调用；返回 true 表示已接管（如弹出独立窗口），不再继续拖动。 */
  onDetach?: (anchor?: DragResizeAnchor) => boolean
  /** 面板内容；第一个 <header> 作为拖动把手。 */
  children: ReactNode
  minWidth?: number
  minHeight?: number
}

/**
 * .overlay 详情面板框架：全屏居中容器 + 面板，header 按住拖动整个 overlay，
 * 右下角注入统一 resize 手柄。拖动/缩放直接写 DOM style，不经 React state。
 * 拖动忽略表单控件/按钮/链接；模态框（.action-dialog-backdrop 等）打开时面板不抢拖动。
 */
export function DialogPanel({ panelClassName, ariaLabel, onEscape, onDetach, children, minWidth = 320, minHeight = 240 }: DialogPanelProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const onDetachRef = useRef(onDetach)
  useEffect(() => { onDetachRef.current = onDetach })

  useEffect(() => {
    if (!onEscape) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      // 上层有模态框时 Esc 归模态框处理（DialogModal 自带监听），面板不响应。
      if (document.querySelector('.action-dialog-backdrop, .patch-modal-backdrop, .sourcing-result-dialog-backdrop')) return
      onEscape()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onEscape])

  useEffect(() => {
    const panel = panelRef.current
    const overlay = overlayRef.current
    // 纯净模式（独立窗口）下不挂拖动/缩放，渲染也不包 overlay（见下方 return）。
    if (!panel || !overlay || isBareDetached()) return undefined
    const header = panel.querySelector<HTMLElement>('header')
    const cleanupDrag = attachDialogDrag(panel, {
      header,
      moveElement: overlay,
      detach: anchor => onDetachRef.current?.(anchor) ?? false,
      shouldIgnore: target => {
        const el = target as HTMLElement | null
        if (!el) return true
        if (el.closest?.('input, textarea, select, label, button, a')) return true
        if (el.closest?.('.overlay-resize-handle')) return true
        if (document.querySelector('.action-dialog-backdrop, .patch-modal-backdrop')) return true
        return false
      },
    })
    const cleanupResize = attachDialogResize(panel, { minWidth, minHeight })
    return () => {
      cleanupDrag()
      cleanupResize()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="overlay" ref={overlayRef}>
      <section ref={panelRef} className={panelClassName} role={ariaLabel ? 'dialog' : undefined} aria-modal={ariaLabel ? 'true' : undefined} aria-label={ariaLabel}>
        {children}
      </section>
    </div>
  )
}

export interface DialogSheetProps {
  onClose: () => void
  title: ReactNode
  titleId: string
  footer?: ReactNode
  className?: string
  closeDisabled?: boolean
  closeLabel?: string
  initialFocus?: string
  children: ReactNode
}

/** 底部 sheet：窄屏/浮窗形态的模态变体，API 与 DialogModal 对齐。 */
export function DialogSheet({
  onClose,
  title,
  titleId,
  footer,
  className,
  closeDisabled = false,
  closeLabel = '关闭',
  initialFocus,
  children,
}: DialogSheetProps) {
  const dialogRef = useDialogFocus<HTMLElement>(true, { initialFocus })
  useDialogEscape(onClose, closeDisabled)

  return (
    <div
      className="dialog-sheet-backdrop"
      role="presentation"
      onClick={() => { if (!closeDisabled) onClose() }}
    >
      <section
        ref={dialogRef}
        className={cx('dialog-sheet', className)}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={event => event.stopPropagation()}
      >
        <header>
          <h3 id={titleId}>{title}</h3>
          <button className="icon-btn" disabled={closeDisabled} onClick={onClose} title={closeLabel} aria-label={closeLabel}><X /></button>
        </header>
        <div className="dialog-sheet-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </section>
    </div>
  )
}
