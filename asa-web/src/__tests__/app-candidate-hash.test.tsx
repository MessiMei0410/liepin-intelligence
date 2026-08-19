import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../app/App'
import { mockResponse } from './helpers'

// 回归（dogfood P0-1）：名单弹窗点候选人 → URL 变 #candidate=<id> → 详情面板正常打开，
// 不得整页白屏。候选人 ID 口径为 job_candidates 关系行 ID。
const syntheticCandidate = {
  id: 1108, job_id: 137, person_id: 1098,
  raw_status: 'search_shortlisted', clean_stage: 'S1 新增寻访/待复核', flow_bucket: '待复核',
  name: '测试人选', current_company: '示例科技', current_title: '机械工程师', city: '杭州',
  education: '本科', experience: '5年', job: '机械高级工程师', client: '长越科技',
  updated_at: '2026-08-19 10:00:00',
  resume: { summary: '', full_text: '', work_text: '', project_text: '', education_text: '', raw: {} },
  source_links: [], events: [], job_relations: [], sourcing_attributions: [], sourcing_recalls: [],
  source_lineage: [], report_artifacts: [], recommendation_packages: [],
  is_stopped: false, stop_reason: '', stop_reason_code: '', stop_reason_label: '',
}

const stubFetch = () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/health') return Promise.resolve(mockResponse({ ok: true }))
    if (url === '/api/v1/bootstrap') return Promise.resolve(mockResponse({ ok: true, core: { status: 'connected' } }))
    if (url === '/api/v1/dashboard') return Promise.resolve(mockResponse({ ok: true }))
    if (url === '/api/v1/candidates/1108') return Promise.resolve(mockResponse({ ok: true, candidate: syntheticCandidate }))
    if (url.startsWith('/api/v1/candidates')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
    if (url.startsWith('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
    if (url.startsWith('/api/v1/workbench')) return Promise.resolve(mockResponse({ ok: true, version: 'v1', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [] }))
    if (url.startsWith('/api/v1/analytics/')) return Promise.resolve(mockResponse({ ok: true, items: [] }))
    if (url.startsWith('/api/v1/copilot/sessions')) return Promise.resolve(mockResponse({ ok: true, sessions: [] }))
    return Promise.resolve(mockResponse({ ok: true }))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('App #candidate= 路由', () => {
  beforeEach(() => {
    localStorage.clear()
    history.replaceState(null, '', '/asa-app#candidate=1108')
    stubFetch()
  })
  afterEach(() => {
    history.replaceState(null, '', location.pathname)
    vi.unstubAllGlobals()
  })

  it('从 hash 路由打开候选人详情，页面不白屏', async () => {
    render(<App />)
    await act(async () => undefined)
    await act(async () => undefined)
    // 详情面板渲染出候选人姓名（面板头部）
    expect(await screen.findByText('测试人选')).toBeInTheDocument()
    // Agent 主界面仍在（不是整页白屏）
    expect(screen.getByLabelText('Agent 消息')).toBeInTheDocument()
  })

  it('候选人接口 404 时保留主界面并提示错误', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/candidates/1108') return Promise.resolve(mockResponse({ detail: 'candidate not found' }, false, 404))
      if (url === '/api/v1/health') return Promise.resolve(mockResponse({ ok: true }))
      if (url === '/api/v1/bootstrap') return Promise.resolve(mockResponse({ ok: true, core: { status: 'connected' } }))
      if (url === '/api/v1/dashboard') return Promise.resolve(mockResponse({ ok: true }))
      if (url.startsWith('/api/v1/candidates')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
      if (url.startsWith('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
      if (url.startsWith('/api/v1/workbench')) return Promise.resolve(mockResponse({ ok: true, version: 'v1', summary: { pending: 0, running: 0, delivered: 0, total: 0 }, items: [] }))
      if (url.startsWith('/api/v1/analytics/')) return Promise.resolve(mockResponse({ ok: true, items: [] }))
      if (url.startsWith('/api/v1/copilot/sessions')) return Promise.resolve(mockResponse({ ok: true, sessions: [] }))
      return Promise.resolve(mockResponse({ ok: true }))
    }))
    render(<App />)
    await act(async () => undefined)
    await act(async () => undefined)
    expect(screen.getByLabelText('Agent 消息')).toBeInTheDocument()
  })
})
