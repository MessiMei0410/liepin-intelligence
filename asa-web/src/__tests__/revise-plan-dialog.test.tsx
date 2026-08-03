import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RevisePlanDialog } from '../components/RevisePlanDialog'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

describe('修改计划对话框', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('打开后空输入与纯空白均禁止提交', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<RevisePlanDialog onCancel={() => undefined} onSubmit={onSubmit} />)
    const submit = screen.getByRole('button', { name: '确认修改' })
    expect(submit).toBeDisabled()
    await user.type(screen.getByRole('textbox'), '   ')
    expect(submit).toBeDisabled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('确认后以 trim 后的修改意见回调', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<RevisePlanDialog onCancel={() => undefined} onSubmit={onSubmit} />)
    await user.type(screen.getByRole('textbox'), '  优先补充华东候选人  ')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith('优先补充华东候选人')
  })

  it('Esc 与遮罩点击取消，对话框内部点击不取消', async () => {
    const user = userEvent.setup()
    const onCancel = vi.fn()
    const { container } = render(<RevisePlanDialog onCancel={onCancel} onSubmit={() => undefined} />)
    await user.click(screen.getByRole('dialog'))
    expect(onCancel).not.toHaveBeenCalled()
    await user.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalledTimes(1)
    await user.click(container.querySelector('.action-dialog-backdrop') as Element)
    expect(onCancel).toHaveBeenCalledTimes(2)
  })

  it('工作流面板只交接到 Agent，不再从本地对话框调用 revise 接口', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const close = vi.fn()
    const contexts: unknown[] = []
    window.addEventListener('asa:open-agent', event => contexts.push((event as CustomEvent).detail), { once: true })
    render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={close} reload={vi.fn()} openCandidate={() => undefined} archived={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '在 Agent 中讨论策略' }))
    expect(contexts).toEqual([expect.objectContaining({ type: 'workflow', id: 'wf-1' })])
    expect(close).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: plannedWorkflow.goal.title })).toBeInTheDocument()
    const reviseCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/v1/workflows/wf-1/revise'))
    expect(reviseCall).toBeUndefined()
  })
})
