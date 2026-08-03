import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CopilotSurface } from '../copilot/CopilotSurface'
describe('旧 Copilot surface', () => {
  it('只提示迁移到主界面，不唤起浮窗或发送请求', () => {
    const fetchMock = vi.fn()
    const postMessage = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    ;(window as Window & { webkit?: unknown }).webkit = { messageHandlers: { asaNative: { postMessage } } }
    render(<CopilotSurface />)
    expect(screen.getByText('Agent 已迁移到 ASA 主界面')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(postMessage).not.toHaveBeenCalled()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    delete (window as Window & { webkit?: unknown }).webkit
    vi.unstubAllGlobals()
  })
})
