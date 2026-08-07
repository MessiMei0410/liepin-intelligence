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

  it('已有评估点击重新评估时显式 force，生成新版本而非复用缓存', async () => {
    const forcedUrl = `${ASSESSMENT_URL}&force=true`
    const fetchMock = stubFetch((url, init) => {
      if (url === forcedUrl && init?.method === 'POST') return { body: assessmentPayload }
      return undefined
    })
    renderAssessment()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '重新评估' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === forcedUrl && init?.method === 'POST')).toBe(true))
    expect(await screen.findByRole('status')).toHaveTextContent('评估已重新生成')
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

// S6-3：评估区新增三块——「在同龄人里的位置」「动机与时机」「需要核实的问题」。
// band 中文/参照系/样本不足「推测」tag；信号带来源链接 + 无信号如实文案；severity 三档色 + 证据 + 空态。
const s63Doc = {
  ...assessmentDoc,
  assessor_version: 's6-3-v1',
  dimensions: {
    ...assessmentDoc.dimensions,
    percentile: {
      verdict: '同方向（技术市场）±3年参照人群 N=10，该人选落位前 25%，高于多数同方向同龄人。',
      band: 'top25',
      basis: 'fit_score',
      score: 88,
      percentile_rank: 0.8,
      reference: {
        n: 10, direction: '技术市场', years_window: 3, median: 67.5, q25: 56.2, q75: 78.8,
        min: 45, max: 95, sample_sufficient: true, min_n: 8, note: '',
      },
      evidence: [{ type: '知识库', ref: '历史人选库参照系：同方向（技术市场）±3年 样本N=10' }],
      confidence: 'certain',
    },
    motivation: {
      verdict: '当前任职已超其历史平均任期，公司近期有公开融资信号，动的可能性存在。',
      signals: [
        { kind: 'tenure_over_avg', source: '简历工况', summary: '当前任职已 65 个月，明显超过其历史平均任期 46.0 个月', as_of: '2026-07-24' },
        { kind: 'funding', kind_label: '融资', source: '公开信息', summary: '杰华特微电子股份有限公司：公司完成新一轮融资', url: 'https://www.joulwatt.com/news/1', as_of: '2026-07-24' },
      ],
      evidence: [{ type: '公开信息', ref: 'https://www.joulwatt.com/news/1' }],
      confidence: 'certain',
    },
    risks: {
      verdict: '共 2 项需要核实的问题（高 1 项，中 1 项），逐项附证据，请逐条核实后再下判断。',
      items: [
        {
          kind: 'gap',
          risk: '2019.03 至 2020.01 之间有约 9 个月简历空窗，需要核实该期间的经历安排',
          severity: 'high',
          evidence: [
            { type: '简历', ref: '2020.01-至今 某公司 · 工程师' },
            { type: '简历', ref: '2017.06-2019.03 另一家公司 · 工程师' },
          ],
        },
        {
          kind: 'hard_requirement',
          risk: '岗位关键词「多相控制器」在简历中未见，需要核实是否具备相关经验',
          severity: 'medium',
          evidence: [],
        },
      ],
      evidence: [{ type: '简历', ref: '2020.01-至今 某公司 · 工程师' }],
      confidence: 'inferred',
    },
  },
}

const s63Payload = { ...assessmentPayload, assessment: s63Doc }

describe('判人评估区 S6-3 三块（分位/动机/需要核实的问题）', () => {
  it('「在同龄人里的位置」：band 中文 + 参照系 N/方向/年限窗 + 中位分', async () => {
    stubFetch(url => (url === ASSESSMENT_URL ? { body: s63Payload } : undefined))
    renderAssessment()
    const block = await screen.findByRole('region', { name: '在同龄人里的位置' })
    expect(within(block).getByText('前 25%')).toBeInTheDocument()
    expect(within(block).getByText(/参照系：同方向（技术市场）±3年/)).toBeInTheDocument()
    expect(within(block).getByText(/样本 N=/)).toHaveTextContent('样本 N=10')
    expect(within(block).getByText(/中位分 67.5/)).toBeInTheDocument()
    expect(within(block).getByText('确定')).toBeInTheDocument()
    expect(within(block).queryByText('推测 · 参照样本不足')).not.toBeInTheDocument()
  })

  it('「在同龄人里的位置」样本不足：显示「推测 · 参照样本不足」tag 与如实备注', async () => {
    const insufficient = {
      ...s63Payload,
      assessment: {
        ...s63Doc,
        dimensions: {
          ...s63Doc.dimensions,
          percentile: {
            ...s63Doc.dimensions.percentile,
            reference: { ...s63Doc.dimensions.percentile.reference, n: 3, sample_sufficient: false, note: '参照样本不足，结论按推测口径' },
            confidence: 'inferred',
          },
        },
      },
    }
    stubFetch(url => (url === ASSESSMENT_URL ? { body: insufficient } : undefined))
    renderAssessment()
    const block = await screen.findByRole('region', { name: '在同龄人里的位置' })
    expect(within(block).getByText('推测 · 参照样本不足')).toBeInTheDocument()
    expect(within(block).getByText('参照样本不足，结论按推测口径')).toBeInTheDocument()
    expect(within(block).getByText(/样本 N=/)).toHaveTextContent('样本 N=3')
  })

  it('「动机与时机」：信号列表带来源链接与 as_of；公开信息信号可点开来源', async () => {
    stubFetch(url => (url === ASSESSMENT_URL ? { body: s63Payload } : undefined))
    renderAssessment()
    const block = await screen.findByRole('region', { name: '动机与时机' })
    expect(within(block).getByText(/当前任职已 65 个月/)).toBeInTheDocument()
    expect(within(block).getByText(/公司完成新一轮融资/)).toBeInTheDocument()
    const link = within(block).getByRole('link', { name: '来源' })
    expect(link).toHaveAttribute('href', 'https://www.joulwatt.com/news/1')
    expect(link).toHaveAttribute('target', '_blank')
    expect(within(block).getAllByText('2026-07-24').length).toBeGreaterThan(0)
  })

  it('「动机与时机」无信号：如实文案，不编造', async () => {
    const noSignal = {
      ...s63Payload,
      assessment: {
        ...s63Doc,
        dimensions: {
          ...s63Doc.dimensions,
          motivation: {
            verdict: '未见明显变动信号：动机与时机需面谈核实。',
            signals: [],
            evidence: [],
            confidence: 'inferred',
          },
        },
      },
    }
    stubFetch(url => (url === ASSESSMENT_URL ? { body: noSignal } : undefined))
    renderAssessment()
    const block = await screen.findByRole('region', { name: '动机与时机' })
    expect(within(block).getAllByText(/未见明显变动信号/).length).toBeGreaterThan(0)
    expect(within(block).queryByRole('link')).not.toBeInTheDocument()
  })

  it('「需要核实的问题」：severity 三档色 tag + kind 中文 + 证据逐条渲染', async () => {
    stubFetch(url => (url === ASSESSMENT_URL ? { body: s63Payload } : undefined))
    renderAssessment()
    const block = await screen.findByRole('region', { name: '需要核实的问题' })
    const high = within(block).getByText('高')
    expect(high).toHaveClass('tag', 'danger')
    const medium = within(block).getByText('中')
    expect(medium).toHaveClass('tag', 'warn')
    expect(within(block).getByText('空窗')).toBeInTheDocument()
    expect(within(block).getByText('硬条件差距')).toBeInTheDocument()
    expect(within(block).getByText(/9 个月简历空窗/)).toBeInTheDocument()
    expect(within(block).getByText(/多相控制器/)).toBeInTheDocument()
    expect(within(block).getByText('2020.01-至今 某公司 · 工程师')).toBeInTheDocument()
    expect(within(block).getByText(/不构成任何决策建议/)).toBeInTheDocument()
  })

  it('「需要核实的问题」空态：items=[] → 显示「未见需核实的问题」', async () => {
    const empty = {
      ...s63Payload,
      assessment: {
        ...s63Doc,
        dimensions: {
          ...s63Doc.dimensions,
          risks: {
            verdict: '未见需核实的问题（已按简历时间线、任期节奏与岗位硬条件逐项核对）。',
            items: [],
            evidence: [],
            confidence: 'certain',
          },
        },
      },
    }
    stubFetch(url => (url === ASSESSMENT_URL ? { body: empty } : undefined))
    renderAssessment()
    const block = await screen.findByRole('region', { name: '需要核实的问题' })
    expect(within(block).getAllByText(/未见需核实的问题/).length).toBeGreaterThan(0)
    expect(within(block).queryByText('高')).not.toBeInTheDocument()
  })

  it('三块为 null（旧版评估）时不渲染对应区块', async () => {
    stubFetch()
    renderAssessment()
    await screen.findByRole('region', { name: '职业轨迹' })
    expect(screen.queryByRole('region', { name: '在同龄人里的位置' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '动机与时机' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '需要核实的问题' })).not.toBeInTheDocument()
  })
})

describe('判人评估区加载 / 重试 / 长引用', () => {
  it('加载中：status 加载态 + aria-busy，请求完成后移除', async () => {
    let resolveFetch!: (value: Response) => void
    const fetchMock = vi.fn<typeof fetch>(() => new Promise<Response>(resolve => { resolveFetch = resolve }))
    vi.stubGlobal('fetch', fetchMock)
    renderAssessment()
    const loading = screen.getByRole('status')
    expect(loading).toHaveTextContent('评估加载中')
    expect(screen.getByRole('region', { name: '判人评估' })).toHaveAttribute('aria-busy', 'true')
    resolveFetch(mockResponse(assessmentPayload))
    await screen.findByRole('region', { name: '职业轨迹' })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('加载失败：错误文案 + 重新加载按钮，重试成功后渲染评估', async () => {
    let attempts = 0
    stubFetch((url, init) => {
      if (!init?.method && attempts++ === 0) return { body: { detail: 'Core 连接中断，评估服务暂不可用' }, ok: false, status: 502 }
      return undefined
    })
    renderAssessment()
    expect(await screen.findByRole('alert')).toHaveTextContent('Core 连接中断')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '重新加载' }))
    expect(await screen.findByRole('region', { name: '职业轨迹' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('证据按维度分组展示，长引用保留完整出处 title', async () => {
    const longRef = `https://example.com/resume-source?line=${'a'.repeat(240)}`
    const doc = {
      ...assessmentDoc,
      dimensions: {
        ...assessmentDoc.dimensions,
        move_history: {
          ...assessmentDoc.dimensions.move_history,
          evidence: [{ type: '简历', ref: longRef }],
        },
      },
    }
    stubFetch(url => (url === ASSESSMENT_URL ? { body: { ...assessmentPayload, assessment: doc } } : undefined))
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    // S6-5 起默认三桶视图，按维度分组需显式切换
    const user = userEvent.setup()
    await user.click(within(evidence).getByRole('button', { name: '按维度' }))
    expect(within(evidence).getByText(/职业轨迹/)).toBeInTheDocument()
    expect(within(evidence).getByText(/跳槽质量史/)).toBeInTheDocument()
    expect(within(evidence).getAllByRole('listitem')).toHaveLength(3)
    const longItem = within(evidence).getByTitle(longRef)
    expect(longItem).toHaveTextContent(longRef)
    expect(longItem).toHaveClass('assessment-evidence-ref')
  })
})


// S6-5 三桶证据卡：直接证据=维度置信度 certain 且带出处；合理推断=inferred 且带出处；
// 未知项=缺出处/置信度未知的证据 +「需要核实的问题」待核验条目。默认三桶视图，可切回按维度。
const bucketGroup = (evidence: HTMLElement, label: string) => {
  const head = within(evidence).getByText(label)
  return head.closest('.assessment-evidence-group') as HTMLElement
}

describe('判人评估区三桶证据卡（S6-5 直接证据/合理推断/未知项）', () => {
  it('默认三桶视图：certain 维证据进直接证据、inferred 进合理推断，并如实说明归桶口径', async () => {
    stubFetch()
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    expect(within(evidence).getByText(/归桶口径/)).toBeInTheDocument()
    const direct = bucketGroup(evidence, '直接证据')
    expect(within(direct).getAllByRole('listitem')).toHaveLength(2)
    expect(within(direct).getByText(/2021\.03-至今 杰华特/)).toBeInTheDocument()
    const inferred = bucketGroup(evidence, '合理推断')
    expect(within(inferred).getAllByRole('listitem')).toHaveLength(1)
    expect(within(inferred).getByText(/晶丰明源/)).toBeInTheDocument()
  })

  it('空桶如实显示「本桶暂无条目」，不编造', async () => {
    stubFetch()
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    const unknown = bucketGroup(evidence, '未知项')
    expect(within(unknown).queryAllByRole('listitem')).toHaveLength(0)
    expect(within(unknown).getByText('本桶暂无条目。')).toBeInTheDocument()
  })

  it('缺出处或置信度未知的证据按保守口径进未知项', async () => {
    const doc = {
      ...assessmentDoc,
      dimensions: {
        ...assessmentDoc.dimensions,
        trajectory: {
          ...assessmentDoc.dimensions.trajectory,
          confidence: undefined,
          evidence: [{ type: '简历', ref: '' }, { type: '图谱', ref: '杰华特微电子股份有限公司' }],
        },
        move_history: null,
      },
    }
    stubFetch(url => (url === ASSESSMENT_URL ? { body: { ...assessmentPayload, assessment: doc } } : undefined))
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    const unknown = bucketGroup(evidence, '未知项')
    const items = within(unknown).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('未提供出处')
    expect(items[1]).toHaveTextContent('杰华特微电子股份有限公司')
    expect(within(bucketGroup(evidence, '直接证据')).queryAllByRole('listitem')).toHaveLength(0)
  })

  it('「需要核实的问题」条目无论置信度一律进未知项（待核验），不与维度证据重复计数', async () => {
    stubFetch(url => (url === ASSESSMENT_URL ? { body: s63Payload } : undefined))
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    const unknown = bucketGroup(evidence, '未知项')
    const items = within(unknown).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('待核验 · 高风险')
    expect(items[0]).toHaveTextContent(/9 个月简历空窗/)
    expect(items[1]).toHaveTextContent('待核验 · 中风险')
    // 直接证据 = 职业轨迹 2 + 在同龄人里的位置 1 + 动机与时机 1；合理推断 = 跳槽质量史 1
    expect(within(bucketGroup(evidence, '直接证据')).getAllByRole('listitem')).toHaveLength(4)
    expect(within(bucketGroup(evidence, '合理推断')).getAllByRole('listitem')).toHaveLength(1)
  })

  it('三桶 ↔ 按维度视图可切换，aria-pressed 如实回写', async () => {
    stubFetch()
    renderAssessment()
    const evidence = await screen.findByRole('region', { name: '证据' })
    const user = userEvent.setup()
    const bucketsButton = within(evidence).getByRole('button', { name: '三桶视图' })
    const dimensionsButton = within(evidence).getByRole('button', { name: '按维度' })
    expect(bucketsButton).toHaveAttribute('aria-pressed', 'true')
    expect(dimensionsButton).toHaveAttribute('aria-pressed', 'false')
    await user.click(dimensionsButton)
    expect(within(evidence).getByText(/职业轨迹/)).toBeInTheDocument()
    expect(within(evidence).queryByText(/归桶口径/)).not.toBeInTheDocument()
    expect(dimensionsButton).toHaveAttribute('aria-pressed', 'true')
    expect(bucketsButton).toHaveAttribute('aria-pressed', 'false')
    await user.click(bucketsButton)
    expect(within(evidence).getByText(/归桶口径/)).toBeInTheDocument()
    expect(bucketsButton).toHaveAttribute('aria-pressed', 'true')
  })
})
