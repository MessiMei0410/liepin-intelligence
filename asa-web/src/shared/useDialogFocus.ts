import { useEffect, useRef } from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

const focusableElements = (root: HTMLElement): HTMLElement[] =>
  Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))

/** Keeps keyboard focus inside a mounted dialog and returns it to the opener. */
export function useDialogFocus<T extends HTMLElement>(active: boolean) {
  const dialogRef = useRef<T>(null)

  useEffect(() => {
    if (!active) return undefined
    const dialog = dialogRef.current || Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"], [role="alertdialog"]')).at(-1)
    if (!dialog) return undefined
    const activeElement = document.activeElement
    const opener = activeElement instanceof HTMLElement && !dialog.contains(activeElement) ? activeElement : null
    const initialFocus = () => {
      const current = document.activeElement
      if (current instanceof HTMLElement && dialog.contains(current)) return
      const target = dialog.querySelector<HTMLElement>('[data-dialog-initial-focus], [autofocus]')
        || focusableElements(dialog)[0]
      target?.focus()
    }
    queueMicrotask(initialFocus)

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const elements = focusableElements(dialog)
      if (!elements.length) return
      const first = elements[0]
      const last = elements[elements.length - 1]
      const current = document.activeElement
      if (event.shiftKey && (current === first || !dialog.contains(current))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (current === last || !dialog.contains(current))) {
        event.preventDefault()
        first.focus()
      }
    }
    dialog.addEventListener('keydown', onKeyDown)
    return () => {
      dialog.removeEventListener('keydown', onKeyDown)
      if (opener?.isConnected) opener.focus()
    }
  }, [active])

  return dialogRef
}
