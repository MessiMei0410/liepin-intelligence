import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentPageContextBar } from '../agent/AgentPageContextBar'
import { mockResponse } from './helpers'

const stateResponse = (overrides: Record<string, unknown> = {}) => ({
  ok: true,
  active_context: {
    surface: 'liepin', source_label: '猎聘', title: '刘先生', subtitle: '中科时代 · 运动控制软件工程师',
    status: '已同步', connected: true, job_candidate_id: 116, updated_at: '2026-08-04T13:30:00', age_seconds: 8,
  },
  active_context_raw: { surface: 'liepin', context_key: 'liepin:tab-1', instance_id: 'tab-1', job_candidate_id: 116 },
  context_quality: { stale: false, age_seconds: 8 },
  suggested_actions: [],
  ...overrides,
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const expandPageContext = async () => {
  fireEvent.click(await screen.findByRole('button', { name: '显示当前页面识别' }))
}

describe('Agent current page context', () => {
  it('默认折叠为当前页面识别按钮，展开后显示动作区', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(stateResponse())))
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)

    expect(await screen.findByRole('button', { name: '显示当前页面识别' })).toBeInTheDocument()
    expect(screen.queryByText('刘先生')).not.toBeInTheDocument()

    await expandPageContext()

    expect(await screen.findByText('刘先生')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起当前页面识别' }))
    expect(screen.queryByText('刘先生')).not.toBeInTheDocument()
  })

  it('展示猎聘候选人、关联人岗关系和完整候选动作', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(stateResponse())))
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    const bar = await screen.findByRole('region', { name: '当前页面' })
    expect(bar).toHaveTextContent('猎聘')
    expect(bar).toHaveTextContent('刘先生')
    expect(bar).toHaveTextContent('人岗关系 #116')
    expect(screen.getByRole('button', { name: '打开人选' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '评估简历' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '生成推荐报告' })).toBeInTheDocument()
  })

  it('展示 X-SaaS 未定位状态和两种预检动作', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(stateResponse({
      active_context: { surface: 'xsaas', source_label: 'X-SaaS', title: '陈先生', subtitle: '技术/研发经理', connected: true, age_seconds: 12 },
      active_context_raw: { surface: 'xsaas', context_key: 'xsaas:tab-2', instance_id: 'tab-2' },
    }))))
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    expect(await screen.findByText('陈先生')).toBeInTheDocument()
    expect(screen.getByText(/未定位唯一人岗关系/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '入库预检' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '推进预检' })).toBeInTheDocument()
  })

  it('无页面上下文时显示等待识别状态', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({
      ok: true,
      active_context: { surface: 'global', source_label: '通用', connected: false },
      active_context_raw: {},
      context_quality: { quality: 'missing', stale: true },
      suggested_actions: [],
    })))
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    expect(await screen.findByText('尚未识别候选人页面')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '评估简历' })).not.toBeInTheDocument()
  })

  it('显式标记过期页面上下文', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(stateResponse({
      active_context: {
        surface: 'liepin', source_label: '猎聘', title: '过期候选人', connected: false,
        job_candidate_id: 118, age_seconds: 980,
      },
      context_quality: { stale: true, age_seconds: 980 },
    }))))
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    const bar = await screen.findByRole('region', { name: '当前页面' })
    expect(bar).toHaveClass('stale')
    expect(bar).toHaveTextContent('已过期')
  })

  it('补全简历并定位通过 floating action 发送到页面桥', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      if (String(input).endsWith('/api/asa/floating/action')) return mockResponse({ ok: true, status: 'queued', message: '已发送到猎聘页面。' })
      return mockResponse(stateResponse({
        active_context: { surface: 'liepin', source_label: '猎聘', title: '待入库候选人', connected: true },
        active_context_raw: { surface: 'liepin', context_key: 'liepin:tab-3', instance_id: 'tab-3' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    fireEvent.click(await screen.findByRole('button', { name: '补全简历并定位' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (!String(input).endsWith('/api/asa/floating/action')) return false
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>
      return body.action === 'fill_resume'
    })).toBe(true))
    expect(await screen.findByText('已发送到猎聘页面。')).toBeInTheDocument()
  })

  it('已定位人选可启动简历评估并打开完整结果', async () => {
    const openFull = vi.fn()
    const fetchMock = vi.fn<typeof fetch>(async input => String(input).endsWith('/api/agent/candidate-assess')
      ? mockResponse({ status: 'completed', run_id: 'assessment-1', job_candidate_id: 116 })
      : mockResponse(stateResponse()))
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentPageContextBar onOpenFullObject={openFull}/>)
    await expandPageContext()

    fireEvent.click(await screen.findByRole('button', { name: '评估简历' }))
    expect(await screen.findByText('简历评估已完成，可打开人选查看完整结果。')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/agent/candidate-assess'))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '打开人选详情' }))
    expect(openFull).toHaveBeenCalledWith(expect.objectContaining({ type: 'candidate', id: 116 }))
  })

  it('评估排队后自动回读运行结果，不停留在已启动', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.endsWith('/api/agent/candidate-assess')) return mockResponse({ ok: true, status: 'queued', run_id: 'assessment-running' })
      if (url.includes('/api/agent/run?run_id=assessment-running')) return mockResponse({ ok: true, status: 'completed', run_id: 'assessment-running' })
      return mockResponse(stateResponse())
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    fireEvent.click(await screen.findByRole('button', { name: '评估简历' }))
    expect(await screen.findByText('简历评估已完成，可打开人选查看完整结果。')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/agent/run?run_id=assessment-running'))).toBe(true)
  })

  it('缺少唯一人岗关系时不启动评估并提示先定位', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse(stateResponse({
      active_context: { surface: 'liepin', source_label: '猎聘', title: '待定位候选人', connected: true },
      active_context_raw: { surface: 'liepin', context_key: 'liepin:tab-4', instance_id: 'tab-4' },
    })))
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentPageContextBar onOpenFullObject={() => {}}/>)
    await expandPageContext()

    fireEvent.click(await screen.findByRole('button', { name: '评估简历' }))
    expect(await screen.findByText('请先补全简历并定位，再进行简历评估。')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/agent/candidate-assess'))).toBe(false)
  })

  it('生成推荐报告返回明确计划回执并可打开工作流', async () => {
    const openFull = vi.fn()
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      if (String(input).endsWith('/api/asa/floating/action')) {
        const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>
        if (body.action === 'generate_report') return mockResponse({ ok: true, status: 'planned', workflow_id: 'workflow-report-1' })
      }
      return mockResponse(stateResponse())
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AgentPageContextBar onOpenFullObject={openFull}/>)
    await expandPageContext()

    fireEvent.click(await screen.findByRole('button', { name: '生成推荐报告' }))
    expect(await screen.findByText('推荐报告生成计划已建立；启动后会生成匹配分析和嘉驰推荐报告。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '打开生成计划' }))
    expect(openFull).toHaveBeenCalledWith(expect.objectContaining({ type: 'workflow', id: 'workflow-report-1' }))
  })
})
