import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SourcingCandidatesPage } from '../pages/SourcingCandidatesPage'
import { mockResponse } from './helpers'

describe('独立寻访名单来源证据', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('在候选人行内展示本轮状态、渠道、查询词和轮次', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({
      ok: true,
      workflow_id: 'wf-1',
      total: 1,
      limit: 50,
      offset: 0,
      items: [{
        id: 8,
        person_id: 88,
        name: '王**',
        company: '电源科技',
        title: '技术市场经理',
        stage: 'S1 待复核',
        flow_bucket: '初筛',
        recommendation: 'recommended',
        attribution: { channel: 'liepin', source_query: '服务器电源 技术市场', source_round: 'R3', from_workflow: true },
        updated_at: '2026-08-14 10:00:00',
        resume_capture_status: 'complete',
      }],
    }))
    vi.stubGlobal('fetch', fetchMock)

    render(<SourcingCandidatesPage workflowId="wf-1" />)

    expect(await screen.findByText('关键词：服务器电源 技术市场 · R3')).toBeInTheDocument()
    expect(screen.getByText('本轮新增')).toBeInTheDocument()
    expect(screen.getByText('猎聘')).toBeInTheDocument()
    expect(screen.getByText('推荐')).toBeInTheDocument()
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/v1/workflows/wf-1/candidates')
  })
})
