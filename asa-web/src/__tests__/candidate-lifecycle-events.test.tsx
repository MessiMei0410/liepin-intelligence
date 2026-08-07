import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidatePanel } from '../panels/CandidatePanel'
import { lifecycleEventLabel, lifecycleEventTone } from '../shared/format'
import type { CandidateDetail } from '../api'
import { candidateDetail, mockResponse } from './helpers'

const lifecycleUrl = '/api/v1/candidates/1/lifecycle-events'

const recordResponse = {
  ok: true,
  candidate_id: 1,
  event_id: 91,
  followup_task_id: 55,
  already_recorded: false,
  event: { id: 91, event_type: 'interview_scheduled', event_type_label: '面试安排', event_status: 'scheduled', event_time: '2026-08-10 14:00:00', summary: '面试安排：一面客户技术负责人' },
  followup: { id: 55, task_type: 'interview_followup', due_at: '2026-08-12 14:00:00', status: 'open' },
  receipt: { idempotent_replay: false, request_id: 'web_test_1' },
}

describe('生命周期事件记录表单（面试/Offer/入职）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(lifecycleUrl)) return mockResponse(recordResponse)
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const openForm = async () => {
    const user = userEvent.setup()
    const changed = vi.fn()
    render(<CandidatePanel value={candidateDetail} close={() => undefined} changed={changed} />)
    await user.click(screen.getByRole('button', { name: '记录' }))
    await user.click(screen.getByRole('button', { name: '记录面试/Offer/入职' }))
    return { user, changed }
  }

  it('展开表单并列出 6 个契约事件类型，默认"面试安排"', async () => {
    await openForm()
    const select = screen.getByRole('combobox', { name: '事件类型' })
    expect(select).toHaveValue('interview_scheduled')
    expect(within(select).getAllByRole('option').map(option => option.textContent)).toEqual([
      '面试安排', '面试完成', 'Offer 发出', 'Offer 已接受', 'Offer 已拒绝', '确认入职',
    ])
    expect(screen.getByText('只写入时间线并生成跟进待办，不会自动对外沟通。')).toBeInTheDocument()
  })

  it('提交走 lifecycle-events 端点并展示回执、回读刷新', async () => {
    const { user, changed } = await openForm()
    fireEvent.change(screen.getByLabelText('发生时间（选填，默认现在）'), { target: { value: '2026-08-10T14:00' } })
    await user.type(screen.getByLabelText('备注（选填）'), '一面客户技术负责人')
    await user.click(screen.getByRole('button', { name: '记录事件' }))
    expect(await screen.findByText(/面试安排已记录（事件 #91），已生成跟进待办 #55/)).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([input]) => String(input).includes(lifecycleUrl))
    expect(call).toBeDefined()
    const init = call?.[1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ event_type: 'interview_scheduled', occurred_at: '2026-08-10 14:00', notes: '一面客户技术负责人' })
    expect(String(init.headers && (init.headers as Record<string, string>)['Idempotency-Key'])).toContain(lifecycleUrl)
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('时间与备注留空时按默认口径提交', async () => {
    const { user } = await openForm()
    await user.selectOptions(screen.getByRole('combobox', { name: '事件类型' }), 'onboarded')
    await user.click(screen.getByRole('button', { name: '记录事件' }))
    await screen.findByText(/已记录（事件 #91）/)
    const call = fetchMock.mock.calls.find(([input]) => String(input).includes(lifecycleUrl))
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toMatchObject({ event_type: 'onboarded', occurred_at: '', notes: '' })
  })

  it('幂等重放/重复记录时提示未重复写入', async () => {
    fetchMock.mockImplementation(async () => mockResponse({ ...recordResponse, already_recorded: true, receipt: { idempotent_replay: true, request_id: 'web_test_1' } }))
    const { user } = await openForm()
    await user.click(screen.getByRole('button', { name: '记录事件' }))
    expect(await screen.findByText(/此前已记录，未重复写入（事件 #91）/)).toBeInTheDocument()
  })

  it('提交失败展示错误且不触发回读', async () => {
    fetchMock.mockImplementation(async () => mockResponse({ detail: '事件时间格式非法：下周三上午' }, false, 409))
    const { user, changed } = await openForm()
    await user.click(screen.getByRole('button', { name: '记录事件' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('事件时间格式非法：下周三上午')
    expect(changed).not.toHaveBeenCalled()
  })
})

describe('生命周期事件时间线渲染', () => {
  const events = [
    { id: 11, event_type: 'offer_extended', event_status: 'extended', event_time: '2026-08-01 10:00:00', summary: 'Offer 发出：已沟通薪资' },
    { id: 12, event_type: 'interview_completed', event_status: 'passed', event_time: '2026-07-28 15:00:00', summary: '面试完成：一面通过' },
    { id: 13, event_type: 'client_feedback', event_status: 'interviewing', event_time: '2026-07-20 09:00:00', summary: '客户反馈记录' },
  ]

  it('业务时间线区分新事件色调并保留旧 client_feedback 中文标签', async () => {
    const user = userEvent.setup()
    const { container } = render(<CandidatePanel value={{ ...candidateDetail, events } as CandidateDetail} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '记录' }))
    expect(screen.getAllByText('Offer 发出：已沟通薪资').length).toBeGreaterThan(0)
    expect(container.querySelector('.timeline-main .tone-offer')).not.toBeNull()
    expect(container.querySelector('.timeline-main .tone-interview')).not.toBeNull()
    expect(screen.getAllByText('已发出').length).toBeGreaterThan(0)
    // 旧口径 client_feedback（event_status=interviewing）仍中文化可读
    expect(screen.getAllByText('进入面试').length).toBeGreaterThan(0)
  })

  it('summary 缺失时回退到生命周期事件中文标签', async () => {
    const user = userEvent.setup()
    render(
      <CandidatePanel
        value={{ ...candidateDetail, events: [{ id: 14, event_type: 'onboarded', event_status: 'recorded', event_time: '2026-08-02 09:00:00' }] } as CandidateDetail}
        close={() => undefined}
        changed={() => undefined}
      />,
    )
    await user.click(screen.getByRole('button', { name: '记录' }))
    expect(screen.getAllByText('确认入职').length).toBeGreaterThan(0)
  })

  it('事件类型标签与色调映射口径', () => {
    expect(lifecycleEventLabel('interview_scheduled')).toBe('面试安排')
    expect(lifecycleEventLabel('offer_declined')).toBe('Offer 已拒绝')
    expect(lifecycleEventLabel('client_feedback')).toBe('')
    expect(lifecycleEventTone('interview_completed')).toBe('tone-interview')
    expect(lifecycleEventTone('offer_accepted')).toBe('tone-offer')
    expect(lifecycleEventTone('onboarded')).toBe('tone-onboard')
    expect(lifecycleEventTone('liepin_outreach')).toBe('')
  })
})
