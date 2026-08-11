import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { DialogFloating, DialogModal, DialogPanel, DialogSheet } from '../shared/Dialog'

function ModalHarness(props: Partial<Parameters<typeof DialogModal>[0]> = {}) {
  const [open, setOpen] = useState(false)
  return <>
    <button onClick={() => setOpen(true)}>打开</button>
    {open && <DialogModal
      onClose={() => setOpen(false)}
      title="测试弹窗"
      titleId="test-dialog-title"
      icon={<span data-testid="icon" />}
      eyebrow="小字"
      footer={<button>确定</button>}
      {...props}
    >
      <input aria-label="名称" />
    </DialogModal>}
  </>
}

describe('DialogModal', () => {
  it('渲染 header/body/footer 结构，初始焦点进入弹窗', async () => {
    const user = userEvent.setup()
    render(<ModalHarness />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    const dialog = screen.getByRole('dialog', { name: '测试弹窗' })
    expect(dialog).toHaveClass('action-dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByText('小字')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确定' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '关闭' })).toHaveFocus())
  })

  it('点击遮罩关闭并归还焦点给触发元素', async () => {
    const user = userEvent.setup()
    render(<ModalHarness />)
    const opener = screen.getByRole('button', { name: '打开' })
    await user.click(opener)
    await screen.findByRole('dialog')
    fireEvent.click(document.querySelector('.action-dialog-backdrop')!)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('Esc 关闭', async () => {
    const user = userEvent.setup()
    render(<ModalHarness />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    await screen.findByRole('dialog')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('closeDisabled 时遮罩/Esc/关闭按钮全部失效', async () => {
    const user = userEvent.setup()
    render(<ModalHarness closeDisabled />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    await screen.findByRole('dialog')
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(document.querySelector('.action-dialog-backdrop')!)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭' })).toBeDisabled()
  })

  it('closeOnBackdrop: false 时遮罩点击不关闭', async () => {
    const user = userEvent.setup()
    render(<ModalHarness closeOnBackdrop={false} />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    await screen.findByRole('dialog')
    fireEvent.click(document.querySelector('.action-dialog-backdrop')!)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('alert 时 role=alertdialog', async () => {
    const user = userEvent.setup()
    render(<ModalHarness alert />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    expect(screen.getByRole('alertdialog', { name: '测试弹窗' })).toBeInTheDocument()
  })

  it('initialFocus selector 指定初始焦点', async () => {
    const user = userEvent.setup()
    render(<ModalHarness initialFocus="button.primary-action" footer={<button className="primary-action">主操作</button>} />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '主操作' })).toHaveFocus())
  })
})

describe('DialogFloating', () => {
  it('非模态渲染并注入 resize 手柄，Esc 关闭', async () => {
    const onClose = vi.fn()
    render(<DialogFloating onClose={onClose} title="名单" ariaLabel="候选名单" icon={<span />}>
      <p>内容</p>
    </DialogFloating>)
    const dialog = screen.getByRole('dialog', { name: '候选名单' })
    expect(dialog).toHaveAttribute('aria-modal', 'false')
    expect(dialog.querySelector('.overlay-resize-handle')).toBeInTheDocument()
    expect(dialog.querySelector('.candidate-dialog-head')).toBeInTheDocument()
    await waitFor(() => expect(dialog).toHaveFocus())
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('卸载后焦点归还之前的元素', async () => {
    const user = userEvent.setup()
    function Harness() {
      const [open, setOpen] = useState(false)
      return <>
        <button onClick={() => setOpen(true)}>打开浮窗</button>
        {open && <DialogFloating onClose={() => setOpen(false)} title="名单" ariaLabel="候选名单"><p>内容</p></DialogFloating>}
      </>
    }
    render(<Harness />)
    const opener = screen.getByRole('button', { name: '打开浮窗' })
    await user.click(opener)
    const dialog = screen.getByRole('dialog')
    await waitFor(() => expect(dialog).toHaveFocus())
    await user.click(screen.getByRole('button', { name: '关闭' }))
    await waitFor(() => expect(opener).toHaveFocus())
  })
})

describe('DialogPanel', () => {
  it('渲染 .overlay 容器与面板，注入 resize 手柄', () => {
    render(<DialogPanel panelClassName="detail-panel">
      <header><h2>面板标题</h2></header>
      <p>面板内容</p>
    </DialogPanel>)
    const overlay = document.querySelector('.overlay')!
    expect(overlay).toBeInTheDocument()
    const panel = overlay.querySelector('.detail-panel')!
    expect(panel).toBeInTheDocument()
    expect(panel.querySelector('.overlay-resize-handle')).toBeInTheDocument()
  })

  it('onEscape：Esc 触发回调，模态框打开时忽略', () => {
    const onEscape = vi.fn()
    render(<>
      <DialogPanel panelClassName="detail-panel" onEscape={onEscape}>
        <header><h2>面板标题</h2></header>
        <p>面板内容</p>
      </DialogPanel>
    </>)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onEscape).toHaveBeenCalledTimes(1)

    // 上层模态框打开时 Esc 归模态框，面板不响应。
    render(<div className="action-dialog-backdrop"><section role="alertdialog" aria-label="确认">确认？</section></div>)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onEscape).toHaveBeenCalledTimes(1)
  })
})

describe('DialogSheet', () => {
  it('渲染底部 sheet，Esc 与遮罩关闭', async () => {
    const onClose = vi.fn()
    render(<DialogSheet onClose={onClose} title="底部面板" titleId="sheet-title"><p>内容</p></DialogSheet>)
    expect(screen.getByRole('dialog', { name: '底部面板' })).toHaveClass('dialog-sheet')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(document.querySelector('.dialog-sheet-backdrop')!)
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
