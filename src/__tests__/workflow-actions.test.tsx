import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkflowPanel } from '../main'
import { mockResponse, plannedWorkflow } from './helpers'

const renderPanel = (reload = vi.fn()) => {
  render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)
  return { reload }
}

describe('工作流动作反馈', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('点击启动后按钮立即进入 loading/disabled，完成后原地刷新', async () => {
    let release: (value: Response) => void = () => undefined
    // R7：动作成功后先打 summary 再按需 reload；summary 调用立即应答，动作调用仍由 release 控制以覆盖 in-flight 状态。
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).includes('/summary')
      ? Promise.resolve(mockResponse({ ok: true }))
      : new Promise<Response>(resolve => { release = resolve })))
    const user = userEvent.setup()
    const { reload } = renderPanel()
    const start = screen.getByRole('button', { name: '启动' })
    await user.click(start)
    expect(start).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
    release(mockResponse({ ok: true }))
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: '启动' })).toBeEnabled()
  })

  it('接口失败时显示可读错误文案且按钮恢复可用', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse({ detail: '工作流已在运行中' }, false, 409)))
    const user = userEvent.setup()
    renderPanel()
    const start = screen.getByRole('button', { name: '启动' })
    await user.click(start)
    expect(await screen.findByText('工作流已在运行中')).toBeInTheDocument()
    expect(start).toBeEnabled()
  })
})
