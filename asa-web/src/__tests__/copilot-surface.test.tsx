import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { CopilotSurface } from '../copilot/CopilotSurface'
import { mockResponse } from './helpers'

const contextUrl = '/api/asa/floating/context'

// R12-b 转发器契约：surface=copilot 不再渲染 Copilot 对话 UI——
// 有 native bridge 则把上下文发进服务端仲裁后唤起浮窗并关闭本页；
// 无 bridge（浏览器）渲染只读提示，全程零 Copilot 写接口调用。
describe('CopilotSurface 转发器（R12-b）', () => {
  let fetchMock: Mock<typeof fetch>
  let postMessage: ReturnType<typeof vi.fn>
  let closeSpy: ReturnType<typeof vi.spyOn>

  const installBridge = () => {
    postMessage = vi.fn()
    ;(window as Window & { webkit?: unknown }).webkit = { messageHandlers: { asaNative: { postMessage } } }
  }
  const removeBridge = () => {
    delete (window as Window & { webkit?: unknown }).webkit
  }

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    closeSpy = vi.spyOn(window, 'close').mockImplementation(() => undefined)
  })

  afterEach(() => {
    removeBridge()
    vi.unstubAllGlobals()
    closeSpy.mockRestore()
  })

  it('有 native bridge：发布上下文 → showFloating → window.close，零 Copilot 写接口', async () => {
    installBridge()
    render(<CopilotSurface />)
    await waitFor(() => expect(postMessage).toHaveBeenCalledWith({ type: 'showFloating' }))
    expect(closeSpy).toHaveBeenCalled()
    // 唯一的请求是把上下文发进服务端仲裁层，不打任何 Copilot 消息/确认接口
    const calls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(calls).toHaveLength(1)
    expect(calls[0]).toContain(contextUrl)
    expect(calls.filter((url) => url.includes('/api/v1/copilot'))).toHaveLength(0)
    expect(calls.filter((url) => url.includes('/api/agent/copilot'))).toHaveLength(0)
    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body)) as Record<string, unknown>
    expect(body.trigger).toBe('copilot')
    // 页面本身不渲染对话 UI
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('无 native bridge：渲染只读提示，零写请求、不关闭页面', async () => {
    render(<CopilotSurface />)
    expect(await screen.findByText('请在 ASA App 中使用浮窗')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(closeSpy).not.toHaveBeenCalled()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})
