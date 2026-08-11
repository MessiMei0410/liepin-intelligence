import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { useDialogFocus } from '../shared/useDialogFocus'

function TestDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useDialogFocus<HTMLDivElement>(true)
  return <div ref={dialogRef} role="dialog" aria-label="焦点测试">
    <button data-dialog-initial-focus>首个操作</button>
    <button onClick={onClose}>关闭对话框</button>
  </div>
}

function Harness() {
  const [open, setOpen] = useState(false)
  return <><button onClick={() => setOpen(true)}>打开对话框</button>{open && <TestDialog onClose={() => setOpen(false)} />}</>
}

function OptionsHarness({ initialFocus, restoreFocus }: { initialFocus?: string; restoreFocus?: boolean }) {
  const [open, setOpen] = useState(false)
  return <>
    <button onClick={() => setOpen(true)}>打开对话框</button>
    {open && <OptionsDialog initialFocus={initialFocus} restoreFocus={restoreFocus} onClose={() => setOpen(false)} />}
  </>
}

function OptionsDialog({ initialFocus, restoreFocus, onClose }: { initialFocus?: string; restoreFocus?: boolean; onClose: () => void }) {
  const dialogRef = useDialogFocus<HTMLDivElement>(true, { initialFocus, restoreFocus })
  return <div
    ref={dialogRef}
    role="dialog"
    aria-label="选项测试"
    onKeyDown={(event) => { if (event.key === 'Escape') onClose() }}
  >
    <input aria-label="名称输入" />
    <button className="primary-action">主操作</button>
    <button onClick={onClose}>关闭对话框</button>
  </div>
}

describe('对话框焦点闭环', () => {
  it('初始焦点进入对话框，Tab 在首尾循环，关闭后归还触发按钮', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const opener = screen.getByRole('button', { name: '打开对话框' })
    await user.click(opener)

    const first = screen.getByRole('button', { name: '首个操作' })
    const last = screen.getByRole('button', { name: '关闭对话框' })
    await waitFor(() => expect(first).toHaveFocus())

    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(first).toHaveFocus()

    await user.click(last)
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('initialFocus selector 优先于默认的 data-dialog-initial-focus / 首个可聚焦元素', async () => {
    const user = userEvent.setup()
    render(<OptionsHarness initialFocus=".primary-action" />)
    await user.click(screen.getByRole('button', { name: '打开对话框' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '主操作' })).toHaveFocus())
  })

  it('Esc 关闭弹窗时焦点归还触发按钮', async () => {
    const user = userEvent.setup()
    render(<OptionsHarness />)
    const opener = screen.getByRole('button', { name: '打开对话框' })
    await user.click(opener)
    const input = screen.getByRole('textbox', { name: '名称输入' })
    await waitFor(() => expect(input).toHaveFocus())

    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('restoreFocus: false 时关闭后不归还焦点', async () => {
    const user = userEvent.setup()
    render(<OptionsHarness restoreFocus={false} />)
    const opener = screen.getByRole('button', { name: '打开对话框' })
    await user.click(opener)
    await waitFor(() => expect(screen.getByRole('textbox', { name: '名称输入' })).toHaveFocus())

    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(opener).not.toHaveFocus()
  })
})
