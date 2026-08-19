import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, WORKBENCH_LIMIT } from '../api'
import { mockResponse, plannedWorkflow } from './helpers'

describe('typed client 高频接口契约', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('dashboard 返回类型化的 counts 与 workflows 字段', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({
      ok: true,
      counts: { active_jobs: 3, pending_approvals: 1 },
      workflows: [{ workflow_id: 'wf-1', status: 'running', title: '寻访', current_stage: 'sourcing' }],
    })))
    const dashboard = await api.dashboard()
    expect(dashboard.counts?.active_jobs).toBe(3)
    expect(dashboard.workflows?.[0]?.workflow_id).toBe('wf-1')
  })

  it('allCandidates 按 200 条分页读取完整候选人关系', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const offset = Number(new URL(String(input), 'http://asa.local').searchParams.get('offset') || 0)
      const remaining = Math.max(0, 450 - offset)
      return mockResponse({
        items: Array.from({ length: Math.min(200, remaining) }, (_, index) => ({ id: offset + index + 1 })),
        total: 450,
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.allCandidates()

    expect(result.items).toHaveLength(450)
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls.map(call => String(call[0]))).toEqual([
      '/api/v1/candidates?limit=200&offset=0&q=',
      '/api/v1/candidates?limit=200&offset=200&q=',
      '/api/v1/candidates?limit=200&offset=400&q=',
    ])
  })

  it('allJobs 按 200 条分页读取完整岗位列表', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const offset = Number(new URL(String(input), 'http://asa.local').searchParams.get('offset') || 0)
      const remaining = Math.max(0, 401 - offset)
      return mockResponse({
        items: Array.from({ length: Math.min(200, remaining) }, (_, index) => ({ id: offset + index + 1 })),
        total: 401,
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.allJobs()

    expect(result.items).toHaveLength(401)
    expect(fetchMock.mock.calls.map(call => String(call[0]))).toEqual([
      '/api/v1/jobs?limit=200&offset=0&q=',
      '/api/v1/jobs?limit=200&offset=200&q=',
      '/api/v1/jobs?limit=200&offset=400&q=',
    ])
  })

  it('workbench 请求服务端最大窗口并透传截断语义', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({
      ok: true, version: 'v1', summary: { pending: 445, running: 0, delivered: 0, total: 445 },
      items: [], returned_count: 300, truncated: true,
    }))
    vi.stubGlobal('fetch', fetchMock)

    const workbench = await api.workbench()

    expect(String(fetchMock.mock.calls[0][0])).toBe(`/api/v1/workbench?limit=${WORKBENCH_LIMIT}`)
    // 序列化窗口封顶 300 时，服务端必须显式告知截断，首页据此渲染“已加载 X / 共 N”。
    expect(workbench.truncated).toBe(true)
    expect(workbench.returned_count).toBe(300)
  })

  it('workflow 经 zod schema 解析并保留 business_outcome', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ ...plannedWorkflow, business_outcome: 'completed_target_met' })))
    const workflow = await api.workflow('wf-1')
    expect(workflow.business_outcome).toBe('completed_target_met')
    expect(workflow.steps).toHaveLength(2)
  })

  it('workflow payload 漂移时降级透传不抛错（不白屏）', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const broken = { ok: true, goal: plannedWorkflow.goal }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(broken)))
    const workflow = await api.workflow('wf-1')
    expect(warn).toHaveBeenCalledTimes(1)
    expect(workflow).toBe(broken)
  })

  it('preflight 提交 CandidateAction 请求体并解析 token', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ token: 'tok-9', impact: '将推进该候选人', expires_at: '2026-07-22 10:00' }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await api.preflight(7, 'advance')
    expect(result.token).toBe('tok-9')
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/candidate-actions/preflight')
    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))
    expect(body).toMatchObject({ candidate_id: 7, action: 'advance' })
    expect(body.request_id).toMatch(/^web_/)
  })

  it('commit 先经 UI 通道激活 token，再携带幂等键与 preflight_token', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await api.commit(7, 'stop', 'tok-9', '方向不符')
    // 人确认闸门：第一次调用是 activate（同一 token），第二次才是 commit。
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/write-confirmations/activate')
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-9' })
    const commitCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/v1/candidate-actions/commit'))
    expect(commitCall).toBeDefined()
    const init = commitCall?.[1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ candidate_id: 7, action: 'stop', preflight_token: 'tok-9', note: '方向不符' })
    expect(init.headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/api/v1/candidate-actions/commit') })
  })
})
