import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { WorkflowActionConfirmDialog } from '../workflows/WorkflowActionConfirmDialog'

// P7：工作流归档/暂停/继续/停止的确认卡，与候选人操作预检确认链对齐。
describe('WorkflowActionConfirmDialog 工作流写操作确认卡', () => {
  it('暂停：渲染 alertdialog、工作流名与影响说明，必填原因校验后才放行', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(<WorkflowActionConfirmDialog action="pause" workflowTitle="寻访前端工程师" busy={false} onConfirm={onConfirm} onCancel={onCancel} />)

    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toHaveTextContent('暂停寻访')
    expect(dialog).toHaveTextContent('寻访前端工程师')
    expect(dialog).toHaveTextContent('渠道会在当前查询单元结束后停止')

    // 原因为空：内联报错，不触发确认
    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))
    expect(screen.getByRole('alert')).toHaveTextContent('请填写原因说明')
    expect(onConfirm).not.toHaveBeenCalled()

    fireEvent.change(screen.getByRole('textbox', { name: '原因说明' }), { target: { value: ' 客户要求暂停一周 ' } })
    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))
    expect(onConfirm).toHaveBeenCalledWith('客户要求暂停一周')
  })

  it('立即停止寻访：danger 操作，确认链同样要求原因', () => {
    const onConfirm = vi.fn()
    render(<WorkflowActionConfirmDialog action="cancel" workflowTitle="寻访电源专家" busy={false} onConfirm={onConfirm} onCancel={() => undefined} />)

    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toHaveTextContent('立即停止寻访')
    expect(dialog).toHaveTextContent('不可撤销')
    fireEvent.change(screen.getByRole('textbox', { name: '原因说明' }), { target: { value: '岗位已关闭' } })
    fireEvent.click(screen.getByRole('button', { name: '确认停止' }))
    expect(onConfirm).toHaveBeenCalledWith('岗位已关闭')
  })

  it('归档：无需原因，直接确认/取消', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(<WorkflowActionConfirmDialog action="archive" workflowTitle="寻访固晶机工程师" busy={false} onConfirm={onConfirm} onCancel={onCancel} />)

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认归档' }))
    expect(onConfirm).toHaveBeenCalledWith('')

    onConfirm.mockClear()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('提交中禁用全部按钮并展示接口错误', () => {
    render(<WorkflowActionConfirmDialog action="resume" workflowTitle="寻访硬件工程师" busy error="preflight token 已过期" onConfirm={() => undefined} onCancel={() => undefined} />)

    expect(screen.getByRole('button', { name: '提交中…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('preflight token 已过期')
  })
})
