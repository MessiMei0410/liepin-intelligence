import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CompanyCalibrationPanel } from '../panels/CompanyCalibration'
import { mockResponse } from './helpers'

// 核心公司校准面板：队列/进度加载、表单预填、提交回执（版本/changed/幂等重放）、
// 拒绝需备注、加载失败重试。fetch 全部打桩，不触网。

const queuePayload = {
  ok: true,
  status: 'pending',
  items: [
    {
      company_key: '鲁滨逊测试技术',
      company_name: '杭州鲁滨逊测试技术有限公司',
      track: '测试量测设备｜测试/分选/AOI/量测',
      business: '半导体测试机、分选机与后道检测设备研发制造',
      categories: ['半导体设备'],
      status: 'pending',
      status_label: '未校准',
      calibration: null,
    },
    {
      company_key: '刻蚀先锋科技',
      company_name: '苏州刻蚀先锋科技有限公司',
      track: '前道设备｜刻蚀/等离子',
      business: '等离子刻蚀装备',
      categories: ['半导体设备'],
      status: 'needs_review',
      status_label: '待复核',
      calibration: {
        calibration_id: 'ccal_1', company_key: '刻蚀先锋科技', company_name: '苏州刻蚀先锋科技有限公司',
        track: '前道设备', product_lines: ['刻蚀机'], skill_tags: ['刻蚀'],
        level_system: '', no_poach: false, non_compete: false, note: '赛道待确认',
        status: 'needs_review', status_label: '待复核', version: 1,
      },
    },
  ],
  total: 2,
}

const progressPayload = {
  ok: true, target: 50, calibrated: 1, needs_review: 1, rejected: 0, pending: 48, total: 589, ratio: 0.02,
}

const submitResult = {
  ok: true,
  changed: true,
  company_key: '鲁滨逊测试技术',
  company_name: '杭州鲁滨逊测试技术有限公司',
  status: 'calibrated',
  status_label: '已校准',
  version: 1,
  calibration: {
    calibration_id: 'ccal_2', company_key: '鲁滨逊测试技术', company_name: '杭州鲁滨逊测试技术有限公司',
    track: '后道测试设备', product_lines: ['STS8200 测试机', '分选机'], skill_tags: ['半导体设备'],
    level_system: '', no_poach: true, non_compete: false, note: '',
    status: 'calibrated', status_label: '已校准', version: 1,
  },
  progress: { ...progressPayload, calibrated: 2, needs_review: 1, pending: 47, ratio: 0.04 },
}

type FetchHandler = (url: string, init?: RequestInit) => Promise<Response>

function stubFetch(handler: Partial<{ queue: FetchHandler; progress: FetchHandler; submit: FetchHandler }>) {
  return vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    if (url.includes('/api/v1/company-calibrations/progress')) {
      return handler.progress ? handler.progress(url, init) : mockResponse(progressPayload)
    }
    if (url.includes('/api/v1/company-calibrations') && init?.method === 'POST') {
      return handler.submit ? handler.submit(url, init) : mockResponse(submitResult)
    }
    if (url.includes('/api/v1/company-calibrations')) {
      return handler.queue ? handler.queue(url, init) : mockResponse(queuePayload)
    }
    return mockResponse({ ok: false }, false, 404)
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('核心公司校准面板', () => {
  it('加载待校准队列与进度指示', async () => {
    vi.stubGlobal('fetch', stubFetch({}))

    render(<CompanyCalibrationPanel />)

    expect(await screen.findByText('杭州鲁滨逊测试技术有限公司')).toBeInTheDocument()
    expect(screen.getByText('苏州刻蚀先锋科技有限公司')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: /校准进度/ })).toHaveTextContent('已校准 1 家 · 首批目标 50 家')
    expect(screen.getByText(/图谱共 589 家/)).toBeInTheDocument()
    // 状态徽标（过滤 tab 同名，故取全部匹配确认徽标存在）。
    expect(screen.getAllByText('未校准').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('待复核').length).toBeGreaterThanOrEqual(2)
  })

  it('加载失败给出明确重试，重试成功恢复队列', async () => {
    let attempts = 0
    vi.stubGlobal('fetch', stubFetch({
      queue: async () => {
        attempts += 1
        return attempts === 1 ? mockResponse({ detail: '校准服务暂不可用' }, false, 503) : mockResponse(queuePayload)
      },
    }))

    render(<CompanyCalibrationPanel />)

    expect(await screen.findByText('校准服务暂不可用')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('杭州鲁滨逊测试技术有限公司')).toBeInTheDocument()
    expect(attempts).toBe(2)
  })

  it('展开表单预填图谱原值，提交校准后直接采用写入回执并后台对账', async () => {
    const submitted: Array<Record<string, unknown>> = []
    let queueReads = 0
    vi.stubGlobal('fetch', stubFetch({
      queue: async () => {
        queueReads += 1
        return mockResponse(queuePayload)
      },
      submit: async (_url, init) => {
        submitted.push(JSON.parse(String(init?.body || '{}')) as Record<string, unknown>)
        return mockResponse(submitResult)
      },
    }))

    render(<CompanyCalibrationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '校准公司：杭州鲁滨逊测试技术有限公司' }))

    const trackInput = screen.getByRole('textbox', { name: '行业（赛道）' })
    expect(trackInput).toHaveValue('测试量测设备｜测试/分选/AOI/量测')
    expect(screen.getByRole('textbox', { name: '技能标签' })).toHaveValue('半导体设备')
    fireEvent.change(trackInput, { target: { value: '后道测试设备' } })
    fireEvent.change(screen.getByRole('textbox', { name: '产品线' }), { target: { value: 'STS8200 测试机\n分选机' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /禁挖/ }))

    fireEvent.click(screen.getByRole('button', { name: '提交校准' }))

    expect(await screen.findByText(/已保存「杭州鲁滨逊测试技术有限公司」校准（已校准，v1）/)).toBeInTheDocument()
    expect(screen.getByRole('status', { name: /校准进度/ })).toHaveTextContent('已校准 2 家 · 首批目标 50 家')
    await waitFor(() => expect(queueReads).toBe(2))
    expect(submitted).toHaveLength(1)
    const body = submitted[0]
    expect(body.company_name).toBe('杭州鲁滨逊测试技术有限公司')
    expect(body.status).toBe('calibrated')
    expect(body.track).toBe('后道测试设备')
    expect(body.product_lines).toEqual(['STS8200 测试机', '分选机'])
    expect(body.no_poach).toBe(true)
    expect(typeof body.request_id).toBe('string')
  })

  it('写入成功后后台队列永久悬挂，仍立即结束提交并保留真实进度回执', async () => {
    let queueReads = 0
    vi.stubGlobal('fetch', stubFetch({
      queue: async () => {
        queueReads += 1
        if (queueReads === 1) return mockResponse(queuePayload)
        return new Promise<Response>(() => undefined)
      },
    }))

    render(<CompanyCalibrationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '校准公司：杭州鲁滨逊测试技术有限公司' }))
    fireEvent.click(screen.getByRole('button', { name: '提交校准' }))

    expect(await screen.findByText(/已保存「杭州鲁滨逊测试技术有限公司」校准（已校准，v1）/)).toBeInTheDocument()
    expect(screen.getByRole('status', { name: /校准进度/ })).toHaveTextContent('已校准 2 家 · 首批目标 50 家')
    expect(screen.queryByRole('button', { name: '提交校准' })).not.toBeInTheDocument()
    expect(screen.queryByText('校准队列加载中…')).not.toBeInTheDocument()
  })

  it('写入成功后后台队列读取失败，不显示全局错误或推翻成功回执', async () => {
    let queueReads = 0
    vi.stubGlobal('fetch', stubFetch({
      queue: async () => {
        queueReads += 1
        return queueReads === 1
          ? mockResponse(queuePayload)
          : mockResponse({ detail: '后台队列暂不可用' }, false, 503)
      },
    }))

    render(<CompanyCalibrationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '校准公司：杭州鲁滨逊测试技术有限公司' }))
    fireEvent.click(screen.getByRole('button', { name: '提交校准' }))

    expect(await screen.findByText(/已保存「杭州鲁滨逊测试技术有限公司」校准（已校准，v1）/)).toBeInTheDocument()
    await waitFor(() => expect(queueReads).toBe(2))
    expect(screen.queryByText('后台队列暂不可用')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: /校准进度/ })).toHaveTextContent('已校准 2 家 · 首批目标 50 家')
  })

  it('同内容重提与幂等重放的回执如实呈现', async () => {
    vi.stubGlobal('fetch', stubFetch({
      submit: async () => mockResponse({ ...submitResult, changed: false, receipt: { idempotent_replay: true } }),
    }))

    render(<CompanyCalibrationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '校准公司：杭州鲁滨逊测试技术有限公司' }))
    fireEvent.click(screen.getByRole('button', { name: '提交校准' }))

    expect(await screen.findByText(/此前已提交（已校准，v1），已同步最新状态/)).toBeInTheDocument()
  })

  it('拒绝校准必须先填备注原因，未填不发请求', async () => {
    const submitted: unknown[] = []
    vi.stubGlobal('fetch', stubFetch({
      submit: async (_url, init) => {
        submitted.push(init?.body)
        return mockResponse(submitResult)
      },
    }))

    render(<CompanyCalibrationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '校准公司：杭州鲁滨逊测试技术有限公司' }))
    fireEvent.click(screen.getByRole('button', { name: /拒绝（不进消费）/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/备注里写明原因/)
    expect(submitted).toHaveLength(0)

    fireEvent.change(screen.getByRole('textbox', { name: '校准备注' }), { target: { value: '已不在目标行业' } })
    fireEvent.click(screen.getByRole('button', { name: /拒绝（不进消费）/ }))
    await waitFor(() => expect(submitted).toHaveLength(1))
  })

  it('提交失败透出后端中文错误，队列保持可见', async () => {
    vi.stubGlobal('fetch', stubFetch({
      submit: async () => mockResponse({ detail: '未知校准状态：done' }, false, 409),
    }))

    render(<CompanyCalibrationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '校准公司：杭州鲁滨逊测试技术有限公司' }))
    fireEvent.click(screen.getByRole('button', { name: '提交校准' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/未知校准状态：done/)
    expect(screen.getByText('杭州鲁滨逊测试技术有限公司')).toBeInTheDocument()
  })

  it('搜索与状态过滤驱动队列请求参数', async () => {
    const queueUrls: string[] = []
    vi.stubGlobal('fetch', stubFetch({
      queue: async url => {
        queueUrls.push(url)
        return mockResponse({ ...queuePayload, items: [], total: 0 })
      },
    }))

    render(<CompanyCalibrationPanel />)
    await screen.findByText(/当前过滤下没有待校准公司/)

    fireEvent.change(screen.getByRole('textbox', { name: '搜索公司' }), { target: { value: '鲁滨逊' } })
    await waitFor(() => expect(queueUrls.some(url => url.includes('q=%E9%B2%81%E6%BB%A8%E9%80%8A'))).toBe(true))

    fireEvent.click(screen.getByRole('tab', { name: '已校准' }))
    await waitFor(() => expect(queueUrls.some(url => url.includes('status=calibrated'))).toBe(true))
  })
})
