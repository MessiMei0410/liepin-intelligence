import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { App, WorkflowPanel } from '../main'
import type { Workflow } from '../api'
import { candidateDetail, mockResponse, plannedWorkflow } from './helpers'

// R7 轮询优化三组验收：summary 门控（变化才拉详情）、候选人增量刷新（不触发 bootstrap）、SSE 接入与降级。

const runningWorkflow: Workflow = {
  ...plannedWorkflow,
  workflow: { workflow_id: 'wf-live', status: 'running', updated_at: '2026-07-22 10:00:00' },
  progress: { completed: 1, total: 2, ratio: 0.5 },
  steps: [
    { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed', updated_at: '2026-07-22 09:59:00' },
    { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'running', updated_at: '2026-07-22 10:00:00' },
  ],
  events: [{ id: 5, event_type: 'step_started', status: 'running', summary: '正在执行：执行多渠道寻访', created_at: '2026-07-22 10:00:00' }],
}

// 与 runningWorkflow 的 workflowDetailSignature 对齐：status|outcome|progress|pending|最近事件 id。
const summaryPayload = (overrides: Record<string, unknown> = {}) => ({
  ok: true,
  workflow_id: 'wf-live',
  status: 'running',
  business_outcome: null,
  progress: { completed: 1, total: 2, ratio: 0.5 },
  current_stage: 'sourcing',
  pending_approvals: [],
  recent_events: [{ id: 5, event_type: 'step_started', status: 'running', summary: '正在执行：执行多渠道寻访', created_at: '2026-07-22 10:00:00' }],
  ...overrides,
})

const panelFetch = (getSummary: () => Record<string, unknown>) =>
  vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/summary')) return Promise.resolve(mockResponse(getSummary()))
    if (url.includes('/candidates')) return Promise.resolve(mockResponse({ ok: true, items: [], total: 0 }))
    if (url.includes('/steps/')) return Promise.resolve(mockResponse({ ok: true, workflow_id: 'wf-live', step: runningWorkflow.steps[1] }))
    return Promise.resolve(mockResponse({ ok: true }))
  })

const callsMatching = (fetchMock: ReturnType<typeof vi.fn>, pattern: RegExp) =>
  fetchMock.mock.calls.filter(([input]) => pattern.test(String(input))).length

const summaryCalls = (fetchMock: ReturnType<typeof vi.fn>) => callsMatching(fetchMock, /\/summary/)

const renderLivePanel = (reload = vi.fn(async () => undefined)) => {
  render(<WorkflowPanel value={runningWorkflow} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)
  return { reload }
}

class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly url: string
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>()
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener])
  }
  close() {
    this.closed = true
  }
  emit(payload: Record<string, unknown>, id: number) {
    for (const listener of this.listeners.get('workflow') || []) {
      listener({ data: JSON.stringify(payload), lastEventId: String(id) } as MessageEvent)
    }
  }
}

describe('R7 工作流轮询切摘要', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('summary 无变化只打小路由不拉详情，变化后才 reload 且不重复拉', async () => {
    let summary = summaryPayload()
    const fetchMock = panelFetch(() => summary)
    vi.stubGlobal('fetch', fetchMock)
    const { reload } = renderLivePanel()
    await act(async () => { await vi.advanceTimersByTimeAsync(3700) })
    expect(summaryCalls(fetchMock)).toBeGreaterThanOrEqual(3)
    expect(reload).not.toHaveBeenCalled()

    summary = summaryPayload({
      progress: { completed: 2, total: 2, ratio: 1 },
      recent_events: [{ id: 6, event_type: 'step_completed', status: 'completed', summary: '已完成：执行多渠道寻访', created_at: '2026-07-22 10:01:00' }],
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(1300) })
    expect(reload).toHaveBeenCalledTimes(1)

    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    expect(reload).toHaveBeenCalledTimes(1)
  })
})

describe('R7 SSE 接入与降级', () => {
  beforeEach(() => { vi.useFakeTimers(); FakeEventSource.instances = [] })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('SSE 健康时轮询退化为慢兜底，断开后自动回退 1.2s 轮询', async () => {
    const fetchMock = panelFetch(() => summaryPayload())
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('EventSource', FakeEventSource)
    renderLivePanel()
    await act(async () => undefined)
    const stream = FakeEventSource.instances[0]
    expect(stream.url).toContain('/api/v1/events?workflow_id=wf-live')

    act(() => { stream.onopen?.() })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(summaryCalls(fetchMock)).toBe(0)

    act(() => { stream.onerror?.() })
    await act(async () => { await vi.advanceTimersByTimeAsync(1300) })
    expect(summaryCalls(fetchMock)).toBeGreaterThanOrEqual(1)
  })

  it('新事件到达即触发增量比对，重放事件按水位去重', async () => {
    let summary = summaryPayload()
    const fetchMock = panelFetch(() => summary)
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('EventSource', FakeEventSource)
    const { reload } = renderLivePanel()
    await act(async () => undefined)
    const stream = FakeEventSource.instances[0]
    act(() => { stream.onopen?.() })

    // 重放（id ≤ 面板已加载详情的最大事件 id=5）：不触发任何请求。
    act(() => { stream.emit({ id: 5, workflow_id: 'wf-live', event_type: 'step_started' }, 5) })
    await act(async () => undefined)
    expect(summaryCalls(fetchMock)).toBe(0)

    // 新事件：立即触发一次 summary 比对；内容无变化不拉详情。
    act(() => { stream.emit({ id: 6, workflow_id: 'wf-live', event_type: 'step_heartbeat' }, 6) })
    await act(async () => undefined)
    expect(summaryCalls(fetchMock)).toBe(1)
    expect(reload).not.toHaveBeenCalled()

    // 新事件且 summary 实际变化：按需拉一次完整详情。
    summary = summaryPayload({
      progress: { completed: 2, total: 2, ratio: 1 },
      recent_events: [{ id: 7, event_type: 'step_completed', status: 'completed', summary: '已完成：执行多渠道寻访', created_at: '2026-07-22 10:01:00' }],
    })
    act(() => { stream.emit({ id: 7, workflow_id: 'wf-live', event_type: 'step_completed' }, 7) })
    await act(async () => undefined)
    expect(reload).toHaveBeenCalledTimes(1)
  })
})

describe('R7 候选人增量刷新', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('候选人轮询发现变化只刷新详情与列表，不触发 bootstrap/jobs 全量重拉', async () => {
    let detailPayload = { ...candidateDetail, updated_at: '2026-07-22 10:00' }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/bootstrap')) return Promise.resolve(mockResponse({ ok: true, core: { status: 'connected' } }))
      if (url.includes('/api/v1/dashboard')) return Promise.resolve(mockResponse({ ok: true, counts: { candidates: 1 }, workflows: [] }))
      if (url.includes('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
      if (/\/api\/v1\/candidates\/\d+/.test(url)) return Promise.resolve(mockResponse({ candidate: detailPayload }))
      if (url.includes('/api/v1/candidates')) return Promise.resolve(mockResponse({ items: [{ id: 1, person_id: 101, name: '张三', current_company: '示例科技' }], total: 1 }))
      return Promise.resolve(mockResponse({ ok: true }))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await act(async () => undefined)
    fireEvent.click(screen.getByLabelText('打开候选人 张三'))
    await act(async () => undefined)

    detailPayload = { ...detailPayload, updated_at: '2026-07-22 11:00' }
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })

    expect(callsMatching(fetchMock, /\/api\/v1\/bootstrap/)).toBe(1)
    expect(callsMatching(fetchMock, /\/api\/v1\/jobs/)).toBe(1)
    expect(callsMatching(fetchMock, /\/api\/v1\/candidates[?]/)).toBeGreaterThanOrEqual(2)
    expect(callsMatching(fetchMock, /\/api\/v1\/candidates\/1/)).toBeGreaterThanOrEqual(2)
  })
})
