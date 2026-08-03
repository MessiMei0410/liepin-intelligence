import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../app/App'
import type { Workflow } from '../api'
import { mockResponse, plannedWorkflow } from './helpers'

const revisionWorkflow: Workflow = {
  ...plannedWorkflow,
  goal: { ...plannedWorkflow.goal, title: '士兰微｜电源专家｜第2轮寻访 · 修订4' },
  workflow: { ...plannedWorkflow.workflow, workflow_id: 'workflow_revision_4', status: 'waiting_approval' },
  latest_revision_workflow_id: 'workflow_revision_4',
}

describe('App 工作流修订导航', () => {
  afterEach(() => {
    history.replaceState(null, '', location.pathname)
    delete (window as Window & { webkit?: unknown }).webkit
    vi.unstubAllGlobals()
  })

  it('旧工作流 hash 自动切到最新策略', async () => {
    history.replaceState(null, '', `${location.pathname}#workflow=workflow_root`)
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/bootstrap') return Promise.resolve(mockResponse({ ok: true, core: { status: 'connected' } }))
      if (url === '/api/v1/dashboard') return Promise.resolve(mockResponse({ ok: true, workflows: [] }))
      if (url.startsWith('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
      if (url.startsWith('/api/v1/candidates')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
      if (url === '/api/v1/workflows/workflow_root') return Promise.resolve(mockResponse({
        ...plannedWorkflow,
        workflow: { ...plannedWorkflow.workflow, workflow_id: 'workflow_root', status: 'superseded' },
        superseded_by_workflow_id: 'workflow_revision_1',
        latest_revision_workflow_id: 'workflow_revision_4',
      }))
      if (url === '/api/v1/workflows/workflow_revision_4') return Promise.resolve(mockResponse(revisionWorkflow))
      return Promise.resolve(mockResponse({ ok: false }, false, 404))
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await act(async () => undefined)

    expect(await screen.findByText('士兰微｜电源专家｜第2轮寻访 · 修订4')).toBeInTheDocument()
    await waitFor(() => expect(location.hash).toBe('#workflow=workflow_revision_4'))
    expect(fetchMock.mock.calls.filter(([input]) => /^\/api\/v1\/workflows\/[^/?]+$/.test(String(input))).map(([input]) => String(input))).toEqual([
      '/api/v1/workflows/workflow_root',
      '/api/v1/workflows/workflow_revision_4',
    ])
  })

  it('浏览工作流不会自动发布浮窗上下文', async () => {
    history.replaceState(null, '', `${location.pathname}#workflow=workflow_context`)
    const postMessage = vi.fn()
    ;(window as Window & { webkit?: unknown }).webkit = { messageHandlers: { asaNative: { postMessage } } }
    const contextWorkflow: Workflow = {
      ...plannedWorkflow,
      goal: {
        ...plannedWorkflow.goal,
        context: { type: 'job', id: 42, client: '士兰微', job: '电源专家', mode: 'strategy_revision' },
      },
      workflow: { ...plannedWorkflow.workflow, workflow_id: 'workflow_context' },
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/bootstrap') return Promise.resolve(mockResponse({ ok: true, core: { status: 'connected' } }))
      if (url === '/api/v1/dashboard') return Promise.resolve(mockResponse({ ok: true, workflows: [] }))
      if (url.startsWith('/api/v1/jobs')) return Promise.resolve(mockResponse({ items: [{ id: 42, client: '士兰微', title: '电源专家', candidate_count: 0, active_candidate_count: 0 }], total: 1 }))
      if (url.startsWith('/api/v1/candidates')) return Promise.resolve(mockResponse({ items: [], total: 0 }))
      if (url === '/api/v1/workflows/workflow_context') return Promise.resolve(mockResponse(contextWorkflow))
      return Promise.resolve(mockResponse({ ok: false }, false, 404))
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByText(plannedWorkflow.goal.title)).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/asa/floating/context')).toBe(false)
    expect(postMessage).not.toHaveBeenCalled()
  })
})
