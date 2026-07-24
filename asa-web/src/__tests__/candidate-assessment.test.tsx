import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidateAssessment } from '../panels/CandidateAssessment'
import { mockResponse } from './helpers'

// S6-1b 判人评估区（CandidateAssessment）：404 空态 + 做评估、两维全字段渲染、证据列表、
// 置信度 tag、顾问三动作 PATCH 体与状态回显、改判 note、null 占位维度不渲染。
// fetch 全 mock（禁 any），按 URL + method 路由到各端点响应。

const ASSESSMENT_URL = '/api/v1/candidates/1/assessments?job_id=154'
const ADVISOR_URL = '/api/v1/candidates/1/assessments/154/advisor-action'

const assessmentDoc = {
  schema_version: 'assessment_v1',
  candidate_id: 1,
  job_id: 154,
  candidate_name_masked: '张**',
  job_title: '技术市场经理/总监（PC电源）',
  client: '士兰微',
  as_of: '2026-07-24 10:00:00',
  assessor_version: 's6-trajectory-v1',
  model: 'fake-agent-v1',
  dimensions: {
    trajectory: {
      verdict: '从消费电子硬件转到模拟芯片原厂，技术市场线一路上行',
      evidence: [
        { type: '简历', ref: '2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理' },
        { type: '图谱', ref: '杰华特微电子股份有限公司' },
      ],
      confidence: 'certain',
      segments: [
        { company: '杰华特微电子股份有限公司', title: '技术市场经理', period: '2021.03-至今', tier: 'T1', tier_source: 'graph', team: '', report_line: '', note: 'PC电源产品线' },
        { company: '立讯精密', title: '硬件工程师', period: '2013.07-2017.05', tier: 'T2', tier_source: 'inferred', team: '', report_line: '', note: '消费电子' },
      ],
      promotion_pace: 'fast',
      tech_evolution: 'rising',
    },
    move_history: {
      verdict: '两次跳槽均为上升，平台与职责同步抬升',
      evidence: [{ type: '简历', ref: '2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师' }],
      confidence: 'inferred',
      moves: [
        { from: '立讯精密', to: '晶丰明源', direction: 'up', platform: 'up', title_direction: 'up', responsibility_direction: 'up', reason: '消费电子整机转芯片原厂' },
        { from: '晶丰明源', to: '杰华特', direction: 'down', platform: 'lateral', title_direction: 'down', responsibility_direction: 'lateral', reason: '薪酬平移但职责收窄' },
      ],
      current_move: 'lateral',
    },
    percentile: null,
    motivation: null,
    risks: null,
  },
  consultant_summary: '技术市场线轨迹清晰，从整机硬件切到原厂后两次跳槽均上行，当前这单对他偏平移。',
  advisor_action: 'pending',
  advisor_note: '',
}

const assessmentPayload = {
  ok: true,
  candidate_id: 1,
  job_id: 154,
  artifact_id: 'candidate_assessment_1_154',
  title: '判人评估：张** × 技术市场经理 v1',
  content: '# 判人评估',
  created_at: '2026-07-24 10:00:00',
  assessment: assessmentDoc,
}

// PATCH 回包：写回后的最新评估（advisor_action/advisor_note 已更新）。
const advisorResult = (action: string, note = '') => ({
  ...assessmentPayload,
  advisor_action: action,
  advisor_note: note,
  updated_at: '2026-07-24 11:30:00',
  assessment: { ...assessmentDoc, advisor_action: action, advisor_note: note, updated_at: '2026-07-24 11:30:00' },
})

type FetchHandler = (url: string, init?: RequestInit) => { body: unknown; ok?: boolean; status?: number } | undefined

const stubFetch = (handler?: FetchHandler) => {
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const routed = handler ? handler(url, init) : undefined
    const { body = assessmentPayload, ok = true, status = 200 } = routed || {}
    return mockResponse(body, ok, status)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const patchCalls = (fetchMock: ReturnType<typeof stubFetch>) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')

const renderAssessment = () => render(<CandidateAssessment candidateId={1} jobId={154}/>)

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('判人评估区（CandidateAssessment）', () => {
  it('尚无评估：404 空态显示「还没做过评估」，「做评估」按钮调 POST 生成后渲染评估', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url === ASSESSMENT_URL && !init?.method) return { body: { detail: '还没有判人评估' }, ok: false, status: 404 }
      if (url === ASSESSMENT_URL && init?.method === 'POST') return { body: assessmentPayload }
      return undefined
    })
    renderAssessment()
    expect(await screen.findByText('还没做过评估')).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '做评估' }))
    // POST 打对端点（job_id query + request_id body + 幂等头）
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url, init]) => String(url) === ASSESSMENT_URL && init?.method === 'POST')).toHaveLength(1))
    const [, init] = fetchMock.mock.calls.find(([url, i]) => String(url) === ASSESSMENT_URL && i?.method === 'POST')!
    expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^web_/)
    const body = JSON.parse(String(init?.body)) as { request_id?: string }
    expect(body.request_id).toMatch(/^web_/)
    // 生成成功 → 直接渲染评估内容
    expect(await screen.findByRole('region', { name: '职业轨迹' })).toBeInTheDocument()
    expect(screen.queryByText('还没做过评估')).not.toBeInTheDocument()
  })

  it('做评估 409（无简历语料/模型不可用）显示后端中文错误文案', async () => {
    stubFetch((url, init) => {
      if (url === ASSESSMENT_URL && !init?.method) return { body: { detail: '还没有判人评估' }, ok: false, status: 404 }
      if (url === ASSESSMENT_URL && init?.method === 'POST') return { body: { detail: '人选缺少可评估的简历数据，无法生成判人评估' }, ok: false, status: 409 }
      return undefined
    })
    renderAssessment()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '做评估' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('无法生成判人评估')
    // 失败仍停留在空态，可重试
    expect(screen.getByText('还没做过评估')).toBeInTheDocument()
  })

  it('两维全字段渲染：结论/晋升速度/技术栈演进/分段表/逐次移动/当前这单判定/顾问口径摘要', async () => {
    stubFetch()
    renderAssessment()
    // 职业轨迹
    const trajectory = await screen.findByRole('region', { name: '职业轨迹' })
    expect(within(trajectory).getByText('从消费电子硬件转到模拟芯片原厂，技术市场线一路上行')).toBeInTheDocument()
    expect(within(trajectory).getByText('偏快')).toBeInTheDocument()
    expect(within(trajectory).getByText('上升')).toBeInTheDocument()
    const jhwt = within(trajectory).getByText('杰华特微电子股份有限公司').closest('tr')
    expect(jhwt).toHaveTextContent('2021.03-至今')
    expect(jhwt).toHaveTextContent('技术市场经理')
    expect(jhwt).toHaveTextContent('头部')
    const lixun = within(trajectory).getByText('立讯精密').closest('tr')
    expect(lixun).toHaveTextContent('2013.07-2017.05')
    expect(lixun).toHaveTextContent('硬件工程师')
    expect(lixun).toHaveTextContent('腰部')
    expect(lixun).toHaveTextContent('推测') // tier_source=inferred → 推测 tag
    // 跳槽质量史
    const moves = screen.getByRole('region', { name: '跳槽质量史' })
    expect(within(moves).getByText('两次跳槽均为上升，平台与职责同步抬升')).toBeInTheDocument()
    expect(within(moves).getByText('立讯精密 → 晶丰明源')).toBeInTheDocument()
    expect(within(moves).getByText('消费电子整机转芯片原厂')).toBeInTheDocument()
    expect(within(moves).getByText('晶丰明源 → 杰华特')).toBeInTheDocument()
    expect(within(moves).getByText('下降')).toBeInTheDocument()
    expect(within(moves).getByText(/当前这单判定：/)).toHaveTextContent('当前这单判定：平移')
    // 顾问口径摘要
    expect(screen.getByRole('region', { name: '顾问口径摘要' })).toHaveTextContent('技术市场线轨迹清晰')
  })

  it('证据列表：类型中文（简历/图谱）+ ref，标注维度', async () => {
    stubFetch()
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    const items = within(evidence).getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('简历')
    expect(items[0]).toHaveTextContent('2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理')
    expect(items[1]).toHaveTextContent('图谱')
    expect(items[1]).toHaveTextContent('杰华特微电子股份有限公司')
    expect(items[2]).toHaveTextContent('2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师')
  })

  it('置信度 tag：确定/推测按维渲染', async () => {
    stubFetch()
    renderAssessment()
    const trajectory = await screen.findByRole('region', { name: '职业轨迹' })
    expect(within(trajectory).getByText('确定')).toBeInTheDocument()
    const moves = screen.getByRole('region', { name: '跳槽质量史' })
    // 该维证据引用含"推测"的另有 tier_source tag，用 head 区域精确锚定置信度 tag
    const head = within(moves).getByText('推测')
    expect(head).toBeInTheDocument()
    expect(within(trajectory).queryByText('确定', { selector: 'h3' })).not.toBeInTheDocument()
  })

  it('顾问动作「采纳 / 否决」：PATCH 体正确、状态回显、已 action 可再改', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url === ADVISOR_URL && init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as { action?: string }
        return { body: advisorResult(body.action || '') }
      }
      return undefined
    })
    renderAssessment()
    const actions = await screen.findByLabelText('顾问动作')
    expect(within(actions).getByText('顾问动作：待处理')).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(within(actions).getByRole('button', { name: '采纳' }))
    await waitFor(() => expect(patchCalls(fetchMock)).toHaveLength(1))
    const [url, init] = patchCalls(fetchMock)[0]
    expect(url).toBe(ADVISOR_URL)
    expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^web_/)
    const body = JSON.parse(String(init?.body)) as { action?: string; note?: string; request_id?: string }
    expect(body.action).toBe('accepted')
    expect(body.note).toBeUndefined()
    expect(body.request_id).toMatch(/^web_/)
    expect(await within(actions).findByText('顾问动作：已采纳')).toBeInTheDocument()
    // 已 action 的显示当前状态可再改：否决仍可直接点
    await user.click(within(actions).getByRole('button', { name: '否决' }))
    await waitFor(() => expect(patchCalls(fetchMock)).toHaveLength(2))
    const [, secondInit] = patchCalls(fetchMock)[1]
    const secondBody = JSON.parse(String(secondInit?.body)) as { action?: string }
    expect(secondBody.action).toBe('rejected')
    expect(await within(actions).findByText('顾问动作：已否决')).toBeInTheDocument()
  })

  it('改判：展开 note 输入框一并提交，回显已改判与顾问备注', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url === ADVISOR_URL && init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as { action?: string; note?: string }
        return { body: advisorResult(body.action || '', body.note || '') }
      }
      return undefined
    })
    renderAssessment()
    const actions = await screen.findByLabelText('顾问动作')
    const user = userEvent.setup()
    await user.click(within(actions).getByRole('button', { name: '改判' }))
    const textarea = within(actions).getByRole('textbox', { name: '改判口径备注' })
    await user.type(textarea, '当前这单我判上升，不是平移')
    await user.click(within(actions).getByRole('button', { name: '提交改判' }))
    await waitFor(() => expect(patchCalls(fetchMock)).toHaveLength(1))
    const [, init] = patchCalls(fetchMock)[0]
    const body = JSON.parse(String(init?.body)) as { action?: string; note?: string }
    expect(body.action).toBe('modified')
    expect(body.note).toBe('当前这单我判上升，不是平移')
    expect(await within(actions).findByText('顾问动作：已改判')).toBeInTheDocument()
    expect(within(actions).getByText(/顾问备注：当前这单我判上升/)).toBeInTheDocument()
  })

  it('null 占位维度不渲染对应区；维度为 null 时该维区块整体不出现', async () => {
    stubFetch()
    renderAssessment()
    await screen.findByRole('region', { name: '职业轨迹' })
    // percentile/motivation/risks 为 null 占位：不出现对应中文区，也不渲染英文原形
    expect(screen.queryByText(/百分位|动机|风险点/)).not.toBeInTheDocument()
    expect(screen.queryByText(/percentile|motivation|risks/)).not.toBeInTheDocument()
  })

  it('某一维为 null 时只渲染另一维', async () => {
    const partial = {
      ...assessmentPayload,
      assessment: {
        ...assessmentDoc,
        dimensions: { ...assessmentDoc.dimensions, move_history: null },
      },
    }
    stubFetch(url => {
      if (url === ASSESSMENT_URL) return { body: partial }
      return undefined
    })
    renderAssessment()
    expect(await screen.findByRole('region', { name: '职业轨迹' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '跳槽质量史' })).not.toBeInTheDocument()
  })
})
