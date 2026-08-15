import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { LeaveConfirmDialog } from '../components/LeaveConfirmDialog'

describe('LeaveConfirmDialog 离开确认对话框', () => {
  it('展示未保存提示，确认/取消各自回调', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(<LeaveConfirmDialog dirtyCount={1} onConfirm={onConfirm} onCancel={onCancel} />)

    expect(screen.getByRole('alertdialog', { name: '离开当前页面？' })).toBeInTheDocument()
    expect(screen.getByText('当前有填写中的内容尚未提交，离开后将丢失这些内容。')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('多处未提交时展示数量，点击背景关闭', () => {
    const onCancel = vi.fn()
    render(<LeaveConfirmDialog dirtyCount={3} onConfirm={() => {}} onCancel={onCancel} />)
    expect(screen.getByText('当前有 3 处填写中的内容尚未提交，离开后将丢失这些内容。')).toBeInTheDocument()

    fireEvent.click(document.querySelector('.action-dialog-backdrop') as HTMLElement)
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
