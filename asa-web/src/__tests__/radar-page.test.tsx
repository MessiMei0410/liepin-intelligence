import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RadarPage } from '../pages/Radar'
import type { Job } from '../api'
import { mockResponse } from './helpers'

const scanPayload = {
  radar_scan: {
    scan_date: '2026-08-05',
    stats: {},
    signals: [{
      company: '示例科技',
      type: 'hiring',
      summary: '研发岗位增加',
      as_of: '2026-08-05',
      source_urls: ['https://example.com/signal'],
      confidence: 'medium',
      linked_action: 'mapping',
    }],
    ranking: [{ company: '示例科技', score: 82, reason: '招聘异动' }],
  },
}

const jobs: Job[] = [{
  id: 9,
  client: '示例客户',
  title: '电源专家',
  candidate_count: 0,
  active_candidate_count: 0,
}]

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolvePromise: (value: T) => void = () => undefined
  const promise = new Promise<T>(resolve => { resolvePromise = resolve })
  return { promise, resolve: resolvePromise }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('人才雷达页面', () => {
  it('首次加载显示读取状态而非空白', async () => {
    const loading = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(() => loading.promise))

    render(<RadarPage jobs={jobs} />)

    expect(screen.getByRole('status')).toHaveTextContent('正在读取本周人才流动雷达榜单')
    loading.resolve(mockResponse(scanPayload))
    expect(await screen.findByText(/1\. 示例科技/)).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('首次加载失败时提供明确重试，并在重试成功后恢复榜单', async () => {
    let attempts = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => {
      attempts += 1
      return attempts === 1
        ? mockResponse({ detail: '雷达服务暂不可用' }, false, 503)
        : mockResponse(scanPayload)
    }))

    render(<RadarPage jobs={jobs} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('雷达服务暂不可用')
    fireEvent.click(screen.getByRole('button', { name: '重新加载人才雷达' }))

    expect(await screen.findByText(/1\. 示例科技/)).toBeInTheDocument()
    expect(attempts).toBe(2)
  })

  it('人选弹层在请求完成前显示加载中，成功空数组后才显示真实空态', async () => {
    const activateResponse = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/actions/activate')) return activateResponse.promise
      return mockResponse(scanPayload)
    }))

    render(<RadarPage jobs={jobs} />)
    fireEvent.click(await screen.findByRole('button', { name: '库里有这些人' }))

    expect(screen.getByRole('status')).toHaveTextContent('正在读取人才库人选')
    expect(screen.queryByText('人才库里暂时没有这家公司的人选。')).not.toBeInTheDocument()

    activateResponse.resolve(mockResponse({ candidates: [] }))
    expect(await screen.findByText('人才库里暂时没有这家公司的人选。')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('人选弹层加载失败时可原地重试并展示返回的人选', async () => {
    let activateAttempts = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (!url.includes('/actions/activate')) return mockResponse(scanPayload)
      activateAttempts += 1
      if (activateAttempts === 1) return mockResponse({ detail: '人才库查询失败' }, false, 500)
      return mockResponse({
        candidates: [{
          id: 18,
          name_masked: '张**',
          current_title: '电源研发经理',
          current_company: '示例科技',
          tenure: '2022 至今',
          stage: 'S1 待复核',
          last_action_at: '2026-08-04',
        }],
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<RadarPage jobs={jobs} />)
    fireEvent.click(await screen.findByRole('button', { name: '库里有这些人' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('人才库查询失败')
    fireEvent.click(screen.getByRole('button', { name: '重新加载人选' }))

    expect(await screen.findByText('张**')).toBeInTheDocument()
    expect(screen.getByText('电源研发经理')).toBeInTheDocument()
    expect(activateAttempts).toBe(2)
  })

  it('空榜单给出空态说明并支持重新加载', async () => {
    let attempts = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => {
      attempts += 1
      return mockResponse(attempts === 1 ? { radar_scan: { ...scanPayload.radar_scan, ranking: [] } } : scanPayload)
    }))

    render(<RadarPage jobs={jobs} />)

    expect(await screen.findByText('本周未发现达到上榜强度的公开信号。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新加载人才雷达' }))
    expect(await screen.findByText(/1\. 示例科技/)).toBeInTheDocument()
    expect(attempts).toBe(2)
  })

  it('Mapping 发起成功后以状态区提示任务卡', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      if (String(input).includes('/actions/start-mapping') && init?.method === 'POST') {
        return mockResponse({ already_exists: false, artifact_id: 'task-1' })
      }
      return mockResponse(scanPayload)
    }))

    render(<RadarPage jobs={jobs} />)
    expect(await screen.findByText(/1\. 示例科技/)).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: /选择要联动 Mapping 的岗位/ }), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: '发起 Mapping' }))

    expect(await screen.findByRole('status')).toHaveTextContent(/已为「示例科技」建好 Mapping 任务卡：task-1/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('人选弹层支持 Esc 关闭并归还焦点', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/actions/activate')
      ? mockResponse({ candidates: [] })
      : mockResponse(scanPayload)))

    render(<RadarPage jobs={jobs} />)
    const openButton = await screen.findByRole('button', { name: '库里有这些人' })
    openButton.focus()
    fireEvent.click(openButton)

    const dialog = await screen.findByRole('dialog', { name: /库里在 示例科技 的人/ })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(openButton).toHaveFocus()
  })

  it('发起 Mapping 失败只显示动作错误，不清空已经加载的雷达榜单', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      if (String(input).includes('/actions/start-mapping') && init?.method === 'POST') {
        return mockResponse({ detail: 'Mapping 服务暂不可用' }, false, 503)
      }
      return mockResponse(scanPayload)
    }))

    render(<RadarPage jobs={jobs} />)
    expect(await screen.findByText(/1\. 示例科技/)).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: /选择要联动 Mapping 的岗位/ }), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: '发起 Mapping' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Mapping 服务暂不可用'))
    expect(screen.getByText(/1\. 示例科技/)).toBeInTheDocument()
    expect(screen.getByText('研发岗位增加', { exact: false })).toBeInTheDocument()
  })
})
