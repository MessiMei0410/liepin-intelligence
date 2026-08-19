import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../app/App'
import { mockResponse } from './helpers'
import type { Candidate, Job } from '../api'

// P5：首屏分段渲染——jobs 先到先渲染，candidates 慢分页期间显示真实加载态而非假空态。
// P6：SSE workflow 事件风暴下工作台刷新节流合并，在途响应必然落地（不再 refreshId 丢弃）。

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void }
function deferred<T>(): Deferred<T> {
  let resolvePromise: (value: T) => void = () => undefined
  const promise = new Promise<T>(resolve => { resolvePromise = resolve })
  return { promise, resolve: resolvePromise }
}

class FakeEventSource {
  static instances: FakeEventSource[] = []
  listeners: Record<string, Array<() => void>> = {}
  closed = false
  constructor(public url: string) { FakeEventSource.instances.push(this) }
  addEventListener(type: string, handler: () => void) { (this.listeners[type] ||= []).push(handler) }
  close() { this.closed = true }
  emit(type: string) { (this.listeners[type] || []).forEach(handler => handler()) }
}

const job: Job = {
  id: 7,
  title: '电源专家',
  client: '示例客户',
  location: '上海',
  status: '在推',
  lifecycle_stage: 'active_pipeline',
  priority: 'P0',
  candidate_count: 3,
  active_candidate_count: 2,
  updated_at: '2026-08-19T10:00:00',
}

const candidate: Candidate = {
  id: 11,
  person_id: 11,
  name: '张航',
  client: '示例客户',
  job: '电源专家',
  current_company: '示例科技',
  current_title: '高级工程师',
  source_type: 'liepin',
  clean_stage: 'S1 待复核',
  flow_bucket: '初筛',
  updated_at: '2026-08-19T10:00:00',
}

const workbenchPayload = (pending: number) => mockResponse({
  ok: true,
  version: 'v1',
  summary: { pending, running: 0, delivered: 0, total: 0 },
  items: [],
})

const stubBaseFetch = (overrides: (url: string) => Promise<Response> | undefined) =>
  vi.fn<typeof fetch>(async input => {
    const url = String(input)
    const override = overrides(url)
    if (override) return override
    if (url === '/api/v1/bootstrap') return mockResponse({ ok: true, core: { status: 'connected' } })
    if (url === '/api/v1/dashboard') return mockResponse({ ok: true, counts: {} })
    if (url.startsWith('/api/v1/jobs')) return mockResponse({ items: [], total: 0 })
    if (url.startsWith('/api/v1/candidates')) return mockResponse({ items: [], total: 0 })
    if (url.startsWith('/api/v1/workbench')) return workbenchPayload(0)
    if (url.startsWith('/api/v1/analytics/')) return mockResponse({ ok: true, items: [] })
    if (url.startsWith('/api/v1/copilot/sessions')) return mockResponse({ ok: true, sessions: [] })
    return mockResponse({ ok: true })
  })

describe('App 首屏分段渲染与工作台刷新合并', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    history.replaceState(null, '', location.pathname)
    FakeEventSource.instances = []
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('jobs 先到先渲染；candidates 未落地期间人选列表显示加载态而非假空态', async () => {
    const candidatesGate = deferred<Response>()
    vi.stubGlobal('fetch', stubBaseFetch(url => {
      if (url.startsWith('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [job], total: 1 }))
      if (url.startsWith('/api/v1/candidates')) return candidatesGate.promise
      return undefined
    }))

    render(<App />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    // candidates 仍在途中，岗位看板已经可渲染真实数据
    fireEvent.click(screen.getByRole('button', { name: '岗位看板' }))
    await act(async () => undefined)
    expect(screen.getByRole('row', { name: /电源专家/ })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    expect(screen.queryByText('没有符合当前条件的岗位。')).not.toBeInTheDocument()

    // 人选列表：真实加载态，不出现"共 0 个结果/没有符合"的假空态
    fireEvent.click(screen.getByRole('button', { name: '人选列表' }))
    await act(async () => undefined)
    expect(screen.getByRole('status')).toHaveTextContent('正在加载候选人…')
    expect(screen.queryByText('没有符合当前条件的候选人。')).not.toBeInTheDocument()
    expect(screen.queryByText(/共 0 个结果/)).not.toBeInTheDocument()

    // candidates 落地后切换为真实数据
    candidatesGate.resolve(mockResponse({ items: [candidate], total: 1 }))
    await act(async () => undefined)
    expect(screen.getByRole('row', { name: /张航/ })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
  })

  it('SSE 事件风暴下工作台刷新节流合并：在途响应不被丢弃，尾部必有一轮真实执行', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const gates: Deferred<Response>[] = []
    let workbenchCalls = 0
    vi.stubGlobal('fetch', stubBaseFetch(url => {
      if (url.startsWith('/api/v1/workbench')) {
        workbenchCalls += 1
        const gate = deferred<Response>()
        gates.push(gate)
        return gate.promise
      }
      return undefined
    }))

    render(<App />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(workbenchCalls).toBe(1)

    // 首轮落地：待判断 lane 出数
    gates[0].resolve(workbenchPayload(5))
    await act(async () => undefined)
    expect(screen.getByRole('region', { name: '待判断' })).toHaveTextContent('共 5 项')

    // 事件风暴：连续 6 条 workflow 事件，第一条立即触发一轮（在途悬挂），其余进入 trailing
    const source = FakeEventSource.instances[0]
    for (let i = 0; i < 6; i += 1) source.emit('workflow')
    await act(async () => undefined)
    expect(workbenchCalls).toBe(2)

    // trailing 定时器到点：在途未结束，只标记合并，不另起请求
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(workbenchCalls).toBe(2)

    // 在途响应落地：结果必须上屏（旧 refreshId 机制下这一轮会被作废）
    gates[1].resolve(workbenchPayload(111))
    await act(async () => undefined)
    expect(screen.getByRole('region', { name: '待判断' })).toHaveTextContent('共 111 项')

    // 合并的尾部刷新自动补跑一轮，落地后收敛到最新数据
    expect(workbenchCalls).toBe(3)
    gates[2].resolve(workbenchPayload(222))
    await act(async () => undefined)
    expect(screen.getByRole('region', { name: '待判断' })).toHaveTextContent('共 222 项')
    expect(workbenchCalls).toBe(3)
  })
})
