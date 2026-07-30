import { afterEach, describe, expect, it, vi } from 'vitest'
import { publishCopilotContext } from '../copilot/bridge'

describe('Copilot view context protocol', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'webkit')
  })

  it('publishes automatic selection without taking explicit task ownership', async () => {
    Reflect.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { asaNative: { postMessage: vi.fn() } } },
    })
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await publishCopilotContext(
      { type: 'job', id: 10, job: '机械高级工程师', client: '长越科技', page: 'jobs' },
      'selection',
      false,
    )

    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))
    expect(body).toMatchObject({
      protocol_version: 'copilot_context_v2',
      trigger: 'selection',
      explicit: false,
      user_selected: false,
      view_context: {
        page: 'jobs',
        object: { type: 'job', id: 10, label: '机械高级工程师' },
      },
    })
  })

  it('publishes page context with an empty object after detail closes', async () => {
    Reflect.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { asaNative: { postMessage: vi.fn() } } },
    })
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await publishCopilotContext({ type: 'page', page: 'overview' }, 'navigation', false)

    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))
    expect(body.view_context).toEqual({ page: 'overview', object: null })
    expect(body.context.type).toBe('page')
  })
})
