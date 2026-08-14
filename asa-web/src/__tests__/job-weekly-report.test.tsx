import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { JobWeeklyReport } from '../panels/JobWeeklyReport'
import type { JobWeeklyReportListPayload } from '../api'
import { mockResponse } from './helpers'

afterEach(() => vi.unstubAllGlobals())

const brief = (overrides: Record<string, unknown> = {}) => ({
  artifact_id: 'job_weekly_154_2026-08-03',
  title: '岗位周报 士兰微 技术市场经理 2026-08-03 v1',
  version: 1,
  week_start: '2026-08-03',
  week_end: '2026-08-09',
  generated_at: '2026-08-05 10:00:00',
  created_at: '2026-08-05 10:00:00',
  validation_status: 'passed',
  summary: { total: 30, active: 21, recommended: 3, confirmed_this_week: 2, comparison: 'available', risk_count: 2, suggestion_count: 3 },
  ...overrides,
})

const listPayload = (items: ReturnType<typeof brief>[]): JobWeeklyReportListPayload => ({
  ok: true,
  job_id: 154,
  latest: items[0] ?? null,
  items,
})

const reportDocument = () => ({
  job_id: 154,
  job_title: '技术市场经理',
  client: '士兰微',
  version: 1,
  week_start: '2026-08-03',
  week_end: '2026-08-09',
  generated_at: '2026-08-05 10:00:00',
  funnel: { current: { total: 30, active: 21, recommended: 3 }, comparison: 'available' },
  recommendations: { confirmed_this_week: 2 },
  risks: ['风险一', '风险二'],
  suggestions: ['建议一', '建议二', '建议三'],
})

// 按 URL 分发的 fetch 替身：GET 列表 / POST 生成 / GET artifact 详情。
const stubFetch = (options: {
  list: () => JobWeeklyReportListPayload
  onGenerate?: () => void
  generateOk?: boolean
}) => vi.fn<typeof fetch>(async (input, init) => {
  const url = String(input)
  if (url.includes('/weekly-report') && init?.method === 'POST') {
    options.onGenerate?.()
    if (options.generateOk === false) return mockResponse({ detail: '生成冲突' }, false, 409)
    return mockResponse({
      ok: true,
      artifact_id: 'job_weekly_154_2026-08-03',
      version: 1,
      week_start: '2026-08-03',
      week_end: '2026-08-09',
      report: reportDocument(),
    })
  }
  if (url.includes('/weekly-reports')) return mockResponse(options.list())
  if (url.includes('/api/v1/artifacts/')) {
    return mockResponse({
      ok: true,
      artifact: {
        artifact_id: 'job_weekly_154_2026-08-03', artifact_type: 'job_weekly_report',
        title: '岗位周报 士兰微 技术市场经理 2026-08-03 v1', mime_type: 'text/markdown',
        content: '# 岗位周报\n\n## 一、漏斗概览', content_size: 30, content_truncated: false,
        metadata: {}, validation_status: 'passed', created_at: '2026-08-05 10:00:00',
        downloadable: true, download_kind: 'content', file_name: 'weekly.md',
        download_url: '/api/v1/artifacts/job_weekly_154_2026-08-03/file',
      },
    })
  }
  return mockResponse({ detail: 'not found' }, false, 404)
})

describe('岗位周报区块', () => {
  it('空态引导生成，生成后展示最新摘要与历史列表', async () => {
    let generated = false
    vi.stubGlobal('fetch', stubFetch({
      list: () => listPayload(generated ? [brief()] : []),
      onGenerate: () => { generated = true },
    }))
    render(<JobWeeklyReport jobId={154} />)

    expect(await screen.findByText(/还没有岗位周报/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: '生成本周周报' }))

    const section = screen.getByLabelText('岗位周报')
    expect(await within(section).findByText('本周确认推荐')).toBeInTheDocument()
    expect(within(section).getByText('2')).toBeInTheDocument()
    expect(within(section).getByText('2 / 3')).toBeInTheDocument()
    expect(within(section).getByText('2026-08-03 ~ 2026-08-09 · 最新')).toBeInTheDocument()
  })

  it('历史版本列表展示多期，查看完整报告打开产物查看器', async () => {
    vi.stubGlobal('fetch', stubFetch({
      list: () => listPayload([brief({ version: 2 }), brief({ artifact_id: 'job_weekly_154_2026-07-27', week_start: '2026-07-27', week_end: '2026-08-02', title: '岗位周报 士兰微 技术市场经理 2026-07-27 v1' })]),
    }))
    render(<JobWeeklyReport jobId={154} />)

    const section = screen.getByLabelText('岗位周报')
    expect(await within(section).findByText('2 期')).toBeInTheDocument()
    const history = within(section).getByLabelText('历史周报')
    expect(within(history).getByText('2026-08-03 ~ 2026-08-09 · 最新')).toBeInTheDocument()
    expect(within(history).getByText('2026-07-27 ~ 2026-08-02')).toBeInTheDocument()

    await userEvent.setup().click(within(section).getByRole('button', { name: '查看完整报告' }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('岗位周报 士兰微 技术市场经理 2026-08-03 v1')).toBeInTheDocument()
  })

  it('读取失败时可原地重试', async () => {
    let failed = true
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input) => {
      if (String(input).includes('/weekly-reports')) {
        return failed ? mockResponse({ detail: '周报服务暂不可用' }, false, 500) : mockResponse(listPayload([brief()]))
      }
      return mockResponse({ detail: 'not found' }, false, 404)
    }))
    render(<JobWeeklyReport jobId={154} />)

    expect(await screen.findByText('周报服务暂不可用')).toBeInTheDocument()
    failed = false
    await userEvent.setup().click(screen.getByRole('button', { name: /重试/ }))
    expect(await screen.findByText('本周确认推荐')).toBeInTheDocument()
  })

  it('生成失败展示可读错误，不清空已有内容', async () => {
    vi.stubGlobal('fetch', stubFetch({ list: () => listPayload([brief()]), generateOk: false }))
    render(<JobWeeklyReport jobId={154} />)

    expect(await screen.findByText('本周确认推荐')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: '生成本周周报' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('生成冲突')
    expect(screen.getByText('本周确认推荐')).toBeInTheDocument()
  })

  it('生成成功直接采用 POST report，不等待列表回读', async () => {
    const never = new Promise<Response>(() => undefined)
    let generated = false
    let listGets = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/weekly-report') && init?.method === 'POST') {
        generated = true
        return mockResponse({
          ok: true,
          artifact_id: 'job_weekly_154_2026-08-03',
          version: 1,
          week_start: '2026-08-03',
          week_end: '2026-08-09',
          report: reportDocument(),
        })
      }
      if (url.includes('/weekly-reports')) {
        listGets += 1
        return generated ? never : mockResponse(listPayload([]))
      }
      throw new Error(`未预期的请求：${url}`)
    }))
    render(<JobWeeklyReport jobId={154} />)
    await screen.findByText(/还没有岗位周报/)
    await userEvent.setup().click(screen.getByRole('button', { name: '生成本周周报' }))

    expect(await screen.findByText('本周周报已生成并保存为 v1。')).toBeInTheDocument()
    expect(screen.getByText('本周确认推荐')).toBeInTheDocument()
    expect(screen.getByText('2026-08-03 ~ 2026-08-09 · 最新')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '生成本周周报' })).toBeEnabled())
    await waitFor(() => expect(listGets).toBe(2))
  })

  it('生成后的列表回读失败不覆盖已保存周报', async () => {
    let generated = false
    let listGets = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/weekly-report') && init?.method === 'POST') {
        generated = true
        return mockResponse({
          ok: true,
          artifact_id: 'job_weekly_154_2026-08-03',
          version: 1,
          week_start: '2026-08-03',
          week_end: '2026-08-09',
          report: reportDocument(),
        })
      }
      if (url.includes('/weekly-reports')) {
        listGets += 1
        return generated ? mockResponse({ detail: '周报列表回读失败' }, false, 500) : mockResponse(listPayload([]))
      }
      throw new Error(`未预期的请求：${url}`)
    }))
    render(<JobWeeklyReport jobId={154} />)
    await screen.findByText(/还没有岗位周报/)
    await userEvent.setup().click(screen.getByRole('button', { name: '生成本周周报' }))

    expect(await screen.findByText('本周周报已生成并保存为 v1。')).toBeInTheDocument()
    await waitFor(() => expect(listGets).toBe(2))
    expect(screen.getByText('本周确认推荐')).toBeInTheDocument()
    expect(screen.queryByText('周报列表回读失败')).not.toBeInTheDocument()
    expect(screen.queryByText(/周报生成失败/)).not.toBeInTheDocument()
  })
})
