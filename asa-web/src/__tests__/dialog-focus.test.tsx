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
})
