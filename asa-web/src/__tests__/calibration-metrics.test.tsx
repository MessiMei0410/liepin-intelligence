import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { CalibrationMetrics } from '../panels/CalibrationMetrics'
import { mockResponse } from './helpers'

// S6-4 评估校准 · 顾问点头率表格：维度×客户聚合渲染、null 率如实「数据不足」、
// 空态与加载失败降级文案；业务语言锚定（顾问点头率/改判率/否决率）。

const METRICS_URL = '/api/v1/assessments/calibration/metrics'

const metricsPayload = {
  ok: true,
  generated_at: '2026-07-24 14:00:00',
  min_n: 3,
  totals: { assessments: 5, pending: 1, accepted: 3, modified: 1, rejected: 0 },
  groups: [
    {
      client: '士兰微', dimension: 'trajectory', dimension_label: '职业轨迹',
      total: 4, accepted: 3, modified: 1, rejected: 0,
      acceptance_rate: 0.75, modified_rate: 0.25, rejected_rate: 0,
    },
    {
      client: '士兰微', dimension: 'motivation', dimension_label: '动机与时机',
      total: 1, accepted: 1, modified: 0, rejected: 0,
      acceptance_rate: null, modified_rate: null, rejected_rate: null,
    },
  ],
  labels: {
    title: '评估校准 · 顾问点头率', acceptance_rate: '顾问点头率',
    modified_rate: '改判率', rejected_rate: '否决率', insufficient: '数据不足',
  },
}

const stubMetrics = (body: unknown, ok = true, status = 200) => {
  const fetchMock = vi.fn<typeof fetch>(async input => {
    expect(String(input)).toBe(METRICS_URL)
    return mockResponse(body, ok, status)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('评估校准 · 顾问点头率（CalibrationMetrics）', () => {
  it('渲染维度×客户表格，率值按百分比呈现', async () => {
    stubMetrics(metricsPayload)
    render(<CalibrationMetrics/>)
    await waitFor(() => expect(screen.getByText('职业轨迹')).toBeTruthy())
    expect(screen.getByText('评估校准 · 顾问点头率')).toBeTruthy()
    expect(screen.getByText('顾问点头率')).toBeTruthy()
    expect(screen.getByText('改判率')).toBeTruthy()
    expect(screen.getByText('否决率')).toBeTruthy()
    expect(screen.getByText('75%')).toBeTruthy()
    expect(screen.getByText('25%')).toBeTruthy()
    expect(screen.getByText('已评估 5 份 · 已采纳 3 · 已改判 1 · 已否决 0')).toBeTruthy()
  })

  it('数据不足的分组如实呈现「数据不足」，不硬算百分比', async () => {
    stubMetrics(metricsPayload)
    render(<CalibrationMetrics/>)
    await waitFor(() => expect(screen.getByText('动机与时机')).toBeTruthy())
    const cells = screen.getAllByText('数据不足')
    expect(cells.length).toBeGreaterThanOrEqual(3)
  })

  it('空数据与加载失败都有降级文案', async () => {
    stubMetrics({ ok: true, totals: { assessments: 0, pending: 0, accepted: 0, modified: 0, rejected: 0 }, groups: [] })
    const { unmount } = render(<CalibrationMetrics/>)
    await waitFor(() => expect(screen.getByText(/还没有足够的顾问动作数据/)).toBeTruthy())
    unmount()
    stubMetrics({ error: 'boom' }, false, 500)
    render(<CalibrationMetrics/>)
    await waitFor(() => expect(screen.getByText(/校准数据暂时加载失败/)).toBeTruthy())
  })
})
