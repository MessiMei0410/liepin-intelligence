import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RecommendationMetricsCard } from '../panels/RecommendationMetricsCard'
import { mockResponse } from './helpers'

afterEach(() => vi.unstubAllGlobals())

describe('岗位有效推荐率', () => {
  it('展示顾问确认、已评估与主指标口径', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ confirmed_recommendations: 3, assessed_candidates: 12, rate: 0.25 })))
    render(<RecommendationMetricsCard jobId={154} />)
    expect(await screen.findByText('25%')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('顾问确认可推荐人数 / 已完成评估人数')).toBeInTheDocument()
  })

  it('无评估时如实显示数据不足', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ confirmed_recommendations: 0, assessed_candidates: 0, rate: null })))
    render(<RecommendationMetricsCard jobId={154} />)
    expect(await screen.findByText('数据不足')).toBeInTheDocument()
  })

  it('失败时可原地重试', async () => {
    let failed = true
    const fetchMock = vi.fn<typeof fetch>(async () => failed
      ? mockResponse({ detail: '指标服务暂不可用' }, false, 500)
      : mockResponse({ confirmed_recommendations: 1, assessed_candidates: 2, rate: 0.5 }))
    vi.stubGlobal('fetch', fetchMock)
    render(<RecommendationMetricsCard jobId={154} />)
    expect(await screen.findByText('指标服务暂不可用')).toBeInTheDocument()
    failed = false
    await userEvent.setup().click(screen.getByRole('button', { name: /重试/ }))
    expect(await screen.findByText('50%')).toBeInTheDocument()
  })
})
