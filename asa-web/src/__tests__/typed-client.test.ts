import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
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

  it('commit 携带幂等键与 preflight_token', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await api.commit(7, 'stop', 'tok-9', '方向不符')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ candidate_id: 7, action: 'stop', preflight_token: 'tok-9', note: '方向不符' })
    expect(init.headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/api/v1/candidate-actions/commit') })
  })
})
