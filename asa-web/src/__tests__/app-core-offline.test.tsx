import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../app/App'
import { mockResponse } from './helpers'

// Core 离线横幅：App 复用 15s 轮询定时器探测 /api/v1/health，连续 2 次失败才打搅。
const stubFetch = (offline: () => boolean) => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/health') {
      return offline() ? Promise.reject(new Error('connection refused')) : Promise.resolve(mockResponse({ ok: true }))
    }
    if (url === '/api/v1/bootstrap') return Promise.resolve(mockResponse({ ok: true, core: { status: 'connected' } }))
    if (url === '/api/v1/dashboard') return Promise.resolve(mockResponse({ ok: true }))
    if (url.startsWith('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
    if (url.startsWith('/api/v1/candidates')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
    if (url.startsWith('/api/v1/workbench')) return Promise.resolve(mockResponse({ ok: true, version: 'v1', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [] }))
    if (url.startsWith('/api/v1/analytics/')) return Promise.resolve(mockResponse({ ok: true, items: [] }))
    if (url.startsWith('/api/v1/copilot/sessions')) return Promise.resolve(mockResponse({ ok: true, sessions: [] }))
    return Promise.resolve(mockResponse({ ok: true }))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('App Core 离线横幅', () => {
  beforeEach(() => {
    localStorage.clear()
    history.replaceState(null, '', location.pathname)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('连续两次健康检查失败才显示横幅，点击重连成功后消失', async () => {
    let coreDown = true
    stubFetch(() => coreDown)
    render(<App />)
    // 挂载即探测一次：单次失败不打搅
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // 15s 后第二次失败：显示横幅
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(screen.getByRole('alert')).toHaveTextContent('ASA Core 连接中断，检查本机服务后可点击重连')

    // Core 恢复后点击重连：立即重新探测，成功即消失
    coreDown = false
    fireEvent.click(screen.getByRole('button', { name: '重连' }))
    await act(async () => undefined)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('轮询探测恢复后横幅自动消失，单次抖动不重复打搅', async () => {
    let coreDown = true
    stubFetch(() => coreDown)
    render(<App />)
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(screen.getByRole('alert')).toBeInTheDocument()

    // 下一轮轮询成功：横幅自动消失
    coreDown = false
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // 恢复后单次失败不重复打搅，再次连续失败才重新出现
    coreDown = true
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(screen.getByRole('alert')).toHaveTextContent('ASA Core 连接中断，检查本机服务后可点击重连')
  })

  it('单个业务模块加载失败时保留 Agent 主界面并指出故障模块', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url === '/api/v1/bootstrap') return mockResponse({ ok: true, core: { status: 'connected' } })
      if (url === '/api/v1/dashboard') return mockResponse({ ok: true, counts: { active_jobs: 3 } })
      if (url.startsWith('/api/v1/jobs')) return mockResponse({ items: [], total: 0 })
      if (url.startsWith('/api/v1/candidates')) throw new Error('candidate query failed')
      if (url.startsWith('/api/v1/workbench')) return mockResponse({ ok: true, version: 'v1', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [] })
      if (url.startsWith('/api/v1/analytics/')) return mockResponse({ ok: true, items: [] })
      if (url.startsWith('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({ ok: true })
    }))

    render(<App />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(screen.getByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
    expect(screen.getByText(/部分模块加载失败。人选模块：candidate query failed/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'ASA Core 无法连接' })).not.toBeInTheDocument()
  })

  it('首次启动失败进入诊断页后可重连恢复，不改变 Hook 调用顺序', async () => {
    let bootstrapAttempts = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url === '/api/v1/bootstrap') {
        bootstrapAttempts += 1
        if (bootstrapAttempts === 1) throw new Error('bootstrap unavailable')
        return mockResponse({ ok: true, core: { status: 'connected' } })
      }
      if (url === '/api/v1/dashboard') {
        if (bootstrapAttempts === 1) throw new Error('core unavailable')
        return mockResponse({ ok: true, counts: {} })
      }
      if (url.startsWith('/api/v1/jobs') || url.startsWith('/api/v1/candidates')) {
        if (bootstrapAttempts === 1) throw new Error('core unavailable')
        return mockResponse({ items: [], total: 0 })
      }
      if (url.startsWith('/api/v1/workbench')) return mockResponse({ ok: true, version: 'v1', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [] })
      if (url.startsWith('/api/v1/analytics/')) return mockResponse({ ok: true, items: [] })
      if (url.startsWith('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
      return mockResponse({ ok: true })
    }))

    render(<App />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    // 诊断页为懒加载 chunk，假定时器下直接等模块 promise 落地再渲染断言
    await act(async () => { await import('../app/Diagnostics') })
    expect(screen.getByRole('heading', { name: 'ASA Core 无法连接' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '重新连接' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(screen.getByRole('heading', { name: '今天从哪里开始？' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'ASA Core 无法连接' })).not.toBeInTheDocument()
  })
})
