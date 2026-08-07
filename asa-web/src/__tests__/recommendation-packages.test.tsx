import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidatePanel } from '../panels/CandidatePanel'
import type { CandidateDetail, RecommendationPackageDetailPayload } from '../api'
import { candidateDetail, mockResponse } from './helpers'

// 版本化推荐包（recommendation-packages）：
// 1) 候选人详情 recommendation_packages 驱动侧栏版本列表（无确认推荐不渲染区块）；
// 2) 展开查看包内容（候选摘要/人岗证据/风险/待核验问题，按需拉详情）；
// 3) 记录客户反馈（类型枚举 + 内容，幂等提交，回执 + 回读刷新）；
// 4) 确认推荐成功回执如实反映推荐包生成状态。

const packageListItem = {
  package_id: 'recpkg-1', version: 1, status: 'generated', created_at: '2026-08-05 10:00:00', recommendation_id: 3, feedback_count: 0,
}

const packageDetail = (feedback: RecommendationPackageDetailPayload['feedback'] = []): RecommendationPackageDetailPayload => ({
  ok: true,
  ...packageListItem,
  candidate_id: 1,
  person_id: 101,
  job_id: 7,
  summary: {
    name: '张三', current_company: '示例科技', current_title: '前端工程师', city: '上海',
    education: '本科', experience: '8 年', stage: 'S7 已推荐客户/待反馈',
    job: { id: 7, title: '前端工程师', client: 'ACME' },
    recommendation: { id: 3, reason: '硬性要求匹配，候选人意向已确认', confirmed_by: 'consultant', confirmed_at: '2026-08-05 10:00:00' },
  },
  evidence: {
    status: 'ready', assessment_id: 9, fit_score: 82, fit_level: 'A-优先推进', evidence_coverage: 0.9,
    strengths: ['React 经验丰富'], gaps: ['缺少大型团队管理经验'],
  },
  risks: ['薪资期望偏高'],
  verification_questions: ['是否接受远程办公？'],
  feedback,
})

const panelCandidate = (): CandidateDetail => ({
  ...candidateDetail,
  recommendation_packages: [packageListItem],
})

describe('版本化推荐包（recommendation-packages）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/recommendation-packages/recpkg-1')) return mockResponse(packageDetail())
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('侧栏展示推荐包版本列表；无确认推荐时不渲染区块', async () => {
    const first = render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    expect(screen.getByRole('button', { name: '查看推荐包 v1' })).toBeInTheDocument()
    expect(screen.getByText(/客户反馈 0 条/)).toBeInTheDocument()
    first.unmount()

    render(<CandidatePanel value={candidateDetail} close={() => undefined} changed={() => undefined} />)
    expect(screen.queryByRole('button', { name: /查看推荐包/ })).not.toBeInTheDocument()
  })

  it('展开推荐包：展示候选摘要、人岗证据、风险与待核验问题', async () => {
    const user = userEvent.setup()
    render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '查看推荐包 v1' }))

    expect(await screen.findByText('候选摘要')).toBeInTheDocument()
    expect(screen.getByText('前端工程师 @ 示例科技')).toBeInTheDocument()
    expect(screen.getByText(/推荐理由：硬性要求匹配，候选人意向已确认/)).toBeInTheDocument()
    expect(screen.getByText('匹配度 82（A-优先推进）· 证据覆盖 90%')).toBeInTheDocument()
    expect(screen.getByText('React 经验丰富')).toBeInTheDocument()
    expect(screen.getByText('缺少大型团队管理经验')).toBeInTheDocument()
    expect(screen.getByText('薪资期望偏高')).toBeInTheDocument()
    expect(screen.getByText('是否接受远程办公？')).toBeInTheDocument()
    expect(screen.getByText('客户反馈（0 条）')).toBeInTheDocument()
  })

  it('无当前评估时如实呈现证据缺失，不伪造人岗证据', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/recommendation-packages/recpkg-1')) {
        return mockResponse({
          ...packageDetail(),
          evidence: { status: 'no_current_assessment', note: '暂无当前有效的判人评估，人岗匹配证据缺失' },
          risks: [],
          verification_questions: [],
        })
      }
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '查看推荐包 v1' }))
    expect(await screen.findByText('暂无当前有效的判人评估，人岗匹配证据缺失')).toBeInTheDocument()
    expect(screen.getByText('暂无风险记录')).toBeInTheDocument()
    expect(screen.getByText('暂无待核验问题')).toBeInTheDocument()
  })

  it('记录客户反馈：类型+内容提交走幂等写，回执展示并回读刷新反馈列表', async () => {
    let posted = false
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/recommendation-packages/recpkg-1/feedback')) {
        posted = true
        return mockResponse({
          ok: true,
          package_id: 'recpkg-1',
          package_version: 1,
          feedback_id: 11,
          event_id: 55,
          already_recorded: false,
          feedback: { id: 11, feedback_type: 'interview', feedback_type_label: '安排面试', content: '客户安排首轮面试', feedback_time: '2026-08-05 15:00:00', recorded_by: 'consultant' },
        })
      }
      if (url.includes('/api/v1/recommendation-packages/recpkg-1')) {
        return mockResponse(packageDetail(posted ? [
          { id: 11, feedback_type: 'interview', feedback_type_label: '安排面试', content: '客户安排首轮面试', feedback_time: '2026-08-05 15:00:00', recorded_by: 'consultant' },
        ] : []))
      }
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '查看推荐包 v1' }))
    await screen.findByText('候选摘要')

    await user.selectOptions(screen.getByLabelText('反馈类型'), 'interview')
    await user.type(screen.getByLabelText('反馈内容'), '客户安排首轮面试')
    await user.click(screen.getByRole('button', { name: '记录客户反馈' }))

    const feedbackCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/feedback'))
    expect(feedbackCall).toBeDefined()
    const init = feedbackCall?.[1] as RequestInit
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toContain('/feedback')
    expect(JSON.parse(String(init.body))).toMatchObject({ feedback_type: 'interview', content: '客户安排首轮面试' })
    expect(JSON.parse(String(init.body))).toHaveProperty('request_id')

    expect(await screen.findByText(/客户反馈已记录（安排面试），并写入候选人时间线/)).toBeInTheDocument()
    // 回读刷新：反馈条目出现在列表中
    expect(await screen.findByText('客户反馈（1 条）')).toBeInTheDocument()
    expect(screen.getByText('客户安排首轮面试')).toBeInTheDocument()
  })

  it('反馈内容为空时不提交并给出可读提示', async () => {
    const user = userEvent.setup()
    render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '查看推荐包 v1' }))
    await screen.findByText('候选摘要')

    await user.click(screen.getByRole('button', { name: '记录客户反馈' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('请先填写客户反馈内容，再提交。')
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/feedback'))).toBe(false)
  })

  it('反馈提交失败：显示可读回执，不误报成功', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/feedback')) return mockResponse({ detail: '未知客户反馈类型：interview' }, false, 409)
      if (url.includes('/api/v1/recommendation-packages/recpkg-1')) return mockResponse(packageDetail())
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '查看推荐包 v1' }))
    await screen.findByText('候选摘要')

    await user.type(screen.getByLabelText('反馈内容'), '客户安排首轮面试')
    await user.click(screen.getByRole('button', { name: '记录客户反馈' }))
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/客户反馈记录失败：未知客户反馈类型/)).toBeInTheDocument()
    expect(screen.queryByText(/客户反馈已记录/)).not.toBeInTheDocument()
  })

  it('确认推荐成功回执显示推荐包 v1 已生成', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/candidate-actions/preflight')) return mockResponse({ token: 'tok-1', impact: '将标记该候选人为已推荐' })
      if (url.includes('/api/v1/candidate-actions/commit')) return mockResponse({ ok: true })
      if (url.includes('/api/v1/consultant-recommendations/preflight')) return mockResponse({ token: 'consultant-tok-1', impact: '记录顾问确认推荐事实' })
      if (url.includes('/api/v1/consultant-recommendations/commit')) {
        return mockResponse({
          ok: true, confirmed_at: '2026-08-05T14:30:00', reason: '硬性要求匹配',
          package: { package_id: 'recpkg-1', version: 1, status: 'generated', created_at: '2026-08-05 14:30:00', recommendation_id: 3 },
        })
      }
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<CandidatePanel value={panelCandidate()} close={() => undefined} changed={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '已推荐' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.type(within(dialog).getByRole('textbox', { name: /推荐理由/ }), '硬性要求匹配')
    await user.click(within(dialog).getByRole('button', { name: '确认标记已推荐' }))

    expect(await screen.findByText(/推荐理由与确认时间已记录。推荐包 v1 已生成。/)).toBeInTheDocument()
  })
})
