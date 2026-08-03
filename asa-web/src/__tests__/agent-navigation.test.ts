import { describe, expect, it, vi } from 'vitest'
import { AGENT_NAVIGATE_EVENT, openAgentWorkspace } from '../agent/navigation'

describe('Agent navigation', () => {
  it('在站内附着上下文且不再调用原生浮窗', () => {
    const nativePost = vi.fn()
    const fetchMock = vi.fn()
    ;(window as Window & { webkit?: unknown }).webkit = { messageHandlers: { asaNative: { postMessage: nativePost } } }
    vi.stubGlobal('fetch', fetchMock)
    const received: unknown[] = []
    const listener = (event: Event) => received.push((event as CustomEvent).detail)
    window.addEventListener(AGENT_NAVIGATE_EVENT, listener)

    openAgentWorkspace({ type: 'job', id: 154, client: '士兰微', job: '电源专家' })

    expect(received).toEqual([{ type: 'job', id: 154, client: '士兰微', job: '电源专家' }])
    expect(nativePost).not.toHaveBeenCalled()
    expect(fetchMock).not.toHaveBeenCalled()
    window.removeEventListener(AGENT_NAVIGATE_EVENT, listener)
    delete (window as Window & { webkit?: unknown }).webkit
  })
})
