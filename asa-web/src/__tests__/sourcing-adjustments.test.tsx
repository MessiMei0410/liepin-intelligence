import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { SourcingAdjustments } from '../panels/SourcingAdjustments'
import type { SourcingAdjustment, SourcingAdjustmentStatus } from '../api'
import { mockResponse } from './helpers'

afterEach(() => vi.unstubAllGlobals())

const makeItem = (id: number, status: SourcingAdjustmentStatus, overrides: Partial<SourcingAdjustment> = {}): SourcingAdjustment => ({
  id,
  job_id: 154,
  candidate_id: 9,
  candidate_name: '张三',
  adjust_type: 'add_keyword',
  value: '固晶键合',
  rationale: '做固晶但要求 80w，客户预算只有 50w',
  confidence: 0.9,
  status,
  created_at: '2026-08-10T10:00:00',
  ...overrides,
})

const listPayload = (items: SourcingAdjustment[]) => {
  const summary = { pending: items.filter(item => item.status === 'pending').length, applied: items.filter(item => item.status === 'applied').length, ignored: items.filter(item => item.status === 'ignored').length }
  return { ok: true, items, summary }
}

describe('停止备注寻访调整（SourcingAdjustments）', () => {
  it('待应用列表渲染类型徽标、词条、来源候选人与备注摘录', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(listPayload([
      makeItem(1, 'pending'),
      makeItem(2, 'pending', { adjust_type: 'adjust_salary_range', value: '≤60w', rationale: '要求 80w，客户预算只有 50w', candidate_name: '李四' }),
    ]))))
    render(<SourcingAdjustments jobId={154} />)

    expect(await screen.findByText('2 条待处理')).toBeInTheDocument()
    expect(screen.getByText('补充关键词')).toBeInTheDocument()
    expect(screen.getByText('固晶键合')).toBeInTheDocument()
    expect(screen.getByText('做固晶但要求 80w，客户预算只有 50w')).toBeInTheDocument()
    expect(screen.getByText('薪资区间')).toBeInTheDocument()
    expect(screen.getByText('≤60w')).toBeInTheDocument()
    expect(screen.getByText(/来源 李四/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '确认应用' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: '忽略' })).toHaveLength(2)
  })

  it('无调整时展示空态引导', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(listPayload([]))))
    render(<SourcingAdjustments jobId={154} />)
    expect(await screen.findByText(/暂无寻访调整。停止候选人时填写备注/)).toBeInTheDocument()
  })

  it('确认应用走 POST confirm 并刷新列表', async () => {
    let call = 0
    const fetchMock = vi.fn<typeof fetch>(async () => {
      call += 1
      if (call === 1) return mockResponse(listPayload([makeItem(1, 'pending')]))
      if (call === 2) return mockResponse({ ok: true })
      return mockResponse(listPayload([makeItem(1, 'applied', { applied_round: 3, applied_at: '2026-08-10T12:00:00' })]))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<SourcingAdjustments jobId={154} />)

    fireEvent.click(await screen.findByRole('button', { name: '确认应用' }))
    expect(await screen.findByText('已处理 1 条（已应用 1 / 已忽略 0）')).toBeInTheDocument()

    const confirmCall = fetchMock.mock.calls[1]
    expect(String(confirmCall[0])).toContain('/api/v1/sourcing-adjustments/1/confirm')
    expect((confirmCall[1] as RequestInit).method).toBe('POST')
  })

  it('忽略走 POST ignore 并刷新列表', async () => {
    let call = 0
    const fetchMock = vi.fn<typeof fetch>(async () => {
      call += 1
      if (call === 1) return mockResponse(listPayload([makeItem(1, 'pending')]))
      if (call === 2) return mockResponse({ ok: true })
      return mockResponse(listPayload([makeItem(1, 'ignored')]))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<SourcingAdjustments jobId={154} />)

    fireEvent.click(await screen.findByRole('button', { name: '忽略' }))
    expect(await screen.findByText('已处理 1 条（已应用 0 / 已忽略 1）')).toBeInTheDocument()

    const ignoreCall = fetchMock.mock.calls[1]
    expect(String(ignoreCall[0])).toContain('/api/v1/sourcing-adjustments/1/ignore')
    expect((ignoreCall[1] as RequestInit).method).toBe('POST')
  })

  it('已应用/已忽略折叠展示，展开后显示已应用于第 N 轮寻访', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse(listPayload([
      makeItem(1, 'applied', { applied_round: 2, applied_at: '2026-08-10T11:00:00' }),
      makeItem(2, 'ignored', { value: '某公司' }),
      makeItem(3, 'pending', { value: '固晶' }),
    ]))))
    render(<SourcingAdjustments jobId={154} />)

    const toggle = await screen.findByRole('button', { name: /已处理 2 条（已应用 1 \/ 已忽略 1）/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/已应用于第 2 轮寻访/)).not.toBeInTheDocument()

    fireEvent.click(toggle)
    const history = screen.getByLabelText('已处理调整列表')
    expect(within(history).getByText(/已应用于第 2 轮寻访/)).toBeInTheDocument()
    expect(within(history).getByText('已忽略')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })

  it('读取失败时展示错误并可重试', async () => {
    let failed = true
    const fetchMock = vi.fn<typeof fetch>(async () => failed
      ? mockResponse({ detail: '调整服务暂不可用' }, false, 500)
      : mockResponse(listPayload([makeItem(1, 'pending')])))
    vi.stubGlobal('fetch', fetchMock)
    render(<SourcingAdjustments jobId={154} />)

    expect(await screen.findByText('调整服务暂不可用')).toBeInTheDocument()
    failed = false
    fireEvent.click(screen.getByRole('button', { name: /重试/ }))
    expect(await screen.findByRole('button', { name: '确认应用' })).toBeInTheDocument()
  })
})
