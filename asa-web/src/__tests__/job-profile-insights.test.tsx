import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { JobProfileInsights } from '../panels/JobProfileInsights'
import { JobPanel } from '../panels/JobPanel'
import type { JobDetail } from '../api'
import { mockResponse } from './helpers'

// S8 岗位画像区块「这个岗位实际在干什么」：来源人数/职责分布条/工具栈 chips/典型产出/面向客户/
// 示例证据展开（遮罩名+简历片段）/空态（<3 份履历）/「不对」纠正闭环。fetch 全 mock（禁 any），按 URL 路由。

const readyPayload = {
  ok: true,
  job_id: 154,
  status: 'ready',
  source_count: 5,
  min_source_count: 3,
  as_of: '2026-07-24 10:00:00',
  version: 2,
  duties: [
    {
      key: 'pc电源多相控制器', label: 'PC电源多相控制器', count: 3, ratio: 0.6,
      examples: [
        { candidate: '张**', evidence: '负责PC电源多相控制器产品线市场推广' },
        { candidate: '李**', evidence: '负责PC 电源／多相-控制器 客户导入' },
      ],
    },
    { key: 'ac-dc电源芯片', label: 'AC-DC电源芯片', count: 1, ratio: 0.2, examples: [] },
  ],
  tools: [
    {
      key: 'cadenceallegro', label: 'Cadence Allegro', count: 2, ratio: 0.4,
      examples: [{ candidate: '张**', evidence: '使用Cadence Allegro输出参考设计' }],
    },
  ],
  deliverables: [
    { key: '参考设计', label: '参考设计', count: 2, ratio: 0.4, examples: [{ candidate: '王**', evidence: '输出参考设计文档' }] },
  ],
  customers: [
    { key: '服务器电源客户', label: '服务器电源客户', count: 3, ratio: 0.6, examples: [{ candidate: '张**', evidence: '面向服务器电源客户' }] },
  ],
  disputed: [],
  stats: { facts_kept: 12, facts_dropped: 2, disputed_count: 0 },
}

const disputedPayload = {
  ...readyPayload,
  duties: [],
  disputed: [
    { item_type: 'duty', key: 'pc电源多相控制器', label: 'PC电源多相控制器', count: 3, note: '这条不对', disputed_at: '2026-07-24 11:00:00' },
  ],
  stats: { facts_kept: 12, facts_dropped: 2, disputed_count: 1 },
}

const insufficientPayload = {
  ok: true, job_id: 154, status: 'insufficient', source_count: 1, min_source_count: 3,
  as_of: '2026-07-24 10:00:00', version: 1, duties: [], tools: [], deliverables: [], customers: [], disputed: [], stats: {},
}

const notGeneratedPayload = {
  ok: true, job_id: 154, status: 'not_generated', source_count: 0, min_source_count: 3,
  as_of: '', version: 0, duties: [], tools: [], deliverables: [], customers: [], disputed: [], stats: {},
}

const stubFetch = (handler?: (url: string, init?: RequestInit) => unknown) => {
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const body = handler ? handler(url, init) : undefined
    return mockResponse(body === undefined ? readyPayload : body)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const textOf = (text: string) => (_: string, element: Element | null) =>
  element?.textContent === text && !Array.from(element.children).some(child => child.textContent === text)

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('岗位画像区块（JobProfileInsights）', () => {
  it('ready：来源人数 + 职责分布条 + 工具栈 + 典型产出 + 面向客户', async () => {
    stubFetch()
    render(<JobProfileInsights jobId={154} />)
    expect(await screen.findByText('这个岗位实际在干什么')).toBeInTheDocument()
    expect(await screen.findByText(/来源 5 份人选履历/)).toBeInTheDocument()
    expect(screen.getByText(textOf('PC电源多相控制器'))).toBeInTheDocument()
    expect(screen.getAllByText('60% · 3人').length).toBeGreaterThan(0)  // 职责 60%·3人 + 客户 60%·3人
    expect(screen.getByText('20% · 1人')).toBeInTheDocument()
    expect(screen.getByText(textOf('Cadence Allegro ×2'))).toBeInTheDocument()
    expect(screen.getByText(textOf('参考设计'))).toBeInTheDocument()
    expect(screen.getByText(textOf('服务器电源客户'))).toBeInTheDocument()
    expect(screen.getByText('职责分布')).toBeInTheDocument()
    expect(screen.getByText('常用工具栈')).toBeInTheDocument()
    expect(screen.getByText('典型产出')).toBeInTheDocument()
    expect(screen.getByText('面向客户与场景')).toBeInTheDocument()
  })

  it('示例证据展开：遮罩名 + 简历逐字片段', async () => {
    stubFetch()
    render(<JobProfileInsights jobId={154} />)
    const toggle = await screen.findByRole('button', { name: 'PC电源多相控制器' })
    expect(screen.queryByText('负责PC电源多相控制器产品线市场推广')).not.toBeInTheDocument()
    await userEvent.click(toggle)
    expect(await screen.findByText('负责PC电源多相控制器产品线市场推广')).toBeInTheDocument()
    expect(screen.getByText('张**')).toBeInTheDocument()
    expect(screen.getByText('李**')).toBeInTheDocument()
    expect(screen.queryByText('张三')).not.toBeInTheDocument() // 只露遮罩名
  })

  it('空态：履历还太少，学不出画像（insufficient / not_generated 统一口径）', async () => {
    stubFetch(() => insufficientPayload)
    const { unmount } = render(<JobProfileInsights jobId={154} />)
    expect(await screen.findByText(/履历还太少，学不出画像/)).toBeInTheDocument()
    expect(screen.getByText(/已学习 1 份人选履历，至少 3 份/)).toBeInTheDocument()
    unmount()
    stubFetch(() => notGeneratedPayload)
    render(<JobProfileInsights jobId={155} />)
    expect(await screen.findByText(/履历还太少，学不出画像/)).toBeInTheDocument()
  })

  it('「不对」按钮：POST feedback 幂等回写 → 重新拉取 → 条目进入已标记区', async () => {
    const onChanged = vi.fn()
    let disputed = false
    const fetchMock = stubFetch((url, init) => {
      if (url.includes('/profile-insights/feedback') && init?.method === 'POST') {
        disputed = true
        return { ok: true, status: 'disputed', already_disputed: false }
      }
      if (url.includes('/profile-insights')) return disputed ? disputedPayload : readyPayload
      return readyPayload
    })
    render(<JobProfileInsights jobId={154} onChanged={onChanged} />)
    const row = (await screen.findByText(textOf('PC电源多相控制器'))).closest('.job-profile-item') as HTMLElement
    const disputeButton = Array.from(row.querySelectorAll('button')).find(button => button.textContent === '不对') as HTMLButtonElement
    await userEvent.click(disputeButton)
    expect(await screen.findByText(/已记录"PC电源多相控制器"不对/)).toBeInTheDocument()
    const postCalls = fetchMock.mock.calls.filter(([url, init]) => String(url).includes('/profile-insights/feedback') && init?.method === 'POST')
    expect(postCalls).toHaveLength(1)
    const body = JSON.parse(String(postCalls[0][1]?.body))
    expect(body.item_type).toBe('duty')
    expect(body.item_key).toBe('pc电源多相控制器')
    expect(body.item_label).toBe('PC电源多相控制器')
    expect(String(postCalls[0][1]?.headers && (postCalls[0][1].headers as Record<string, string>)['Idempotency-Key'])).toContain('/api/v1/jobs/154/profile-insights/feedback')
    // 重新拉取后：条目不再出现在主列表，进入「顾问已标记不对」留痕区
    await waitFor(() => expect(screen.getByText(/顾问已标记不对（1 条/)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'PC电源多相控制器' })).not.toBeInTheDocument()
    expect(screen.getByText(textOf('PC电源多相控制器 ×3'))).toBeInTheDocument()
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('纠正写入失败时不触发岗位详情回读', async () => {
    const onChanged = vi.fn()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/profile-insights/feedback') && init?.method === 'POST') return mockResponse({ detail: '画像版本已变化，请重试' }, false, 409)
      return mockResponse(readyPayload)
    }))
    render(<JobProfileInsights jobId={154} onChanged={onChanged} />)
    const row = (await screen.findByText(textOf('PC电源多相控制器'))).closest('.job-profile-item') as HTMLElement
    await userEvent.click(Array.from(row.querySelectorAll('button')).find(button => button.textContent === '不对') as HTMLButtonElement)
    expect(await screen.findByText('画像版本已变化，请重试')).toBeInTheDocument()
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('纠正成功直接采用 POST 重算画像，不等待画像与岗位详情回读', async () => {
    const never = new Promise<Response>(() => undefined)
    const onChanged = vi.fn(() => new Promise<void>(() => undefined))
    let wrote = false
    let profileGets = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/profile-insights/feedback') && init?.method === 'POST') {
        wrote = true
        return mockResponse({
          ok: true,
          status: 'disputed',
          item_type: 'duty',
          item_key: 'pc电源多相控制器',
          duties: disputedPayload.duties,
          tools: disputedPayload.tools,
          deliverables: disputedPayload.deliverables,
          customers: disputedPayload.customers,
          disputed: disputedPayload.disputed,
          stats: disputedPayload.stats,
          source_count: disputedPayload.source_count,
          as_of: disputedPayload.as_of,
        })
      }
      if (url.includes('/profile-insights')) {
        profileGets += 1
        return wrote ? never : mockResponse(readyPayload)
      }
      throw new Error(`未预期的请求：${url}`)
    }))
    render(<JobProfileInsights jobId={154} onChanged={onChanged} />)
    const row = (await screen.findByText(textOf('PC电源多相控制器'))).closest('.job-profile-item') as HTMLElement
    await userEvent.click(Array.from(row.querySelectorAll('button')).find(button => button.textContent === '不对') as HTMLButtonElement)

    expect(await screen.findByText(/已记录"PC电源多相控制器"不对/)).toBeInTheDocument()
    expect(screen.getByText(/顾问已标记不对（1 条/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'PC电源多相控制器' })).not.toBeInTheDocument()
    await waitFor(() => expect(profileGets).toBe(2))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
  })

  it('纠正后的后台回读失败不覆盖数据库成功回执', async () => {
    const onChanged = vi.fn(async () => { throw new Error('岗位详情回读失败') })
    let wrote = false
    let profileGets = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/profile-insights/feedback') && init?.method === 'POST') {
        wrote = true
        return mockResponse({
          ok: true,
          status: 'disputed',
          duties: disputedPayload.duties,
          tools: disputedPayload.tools,
          deliverables: disputedPayload.deliverables,
          customers: disputedPayload.customers,
          disputed: disputedPayload.disputed,
          stats: disputedPayload.stats,
          source_count: disputedPayload.source_count,
          as_of: disputedPayload.as_of,
        })
      }
      if (url.includes('/profile-insights')) {
        profileGets += 1
        return wrote ? mockResponse({ detail: '画像回读失败' }, false, 500) : mockResponse(readyPayload)
      }
      throw new Error(`未预期的请求：${url}`)
    }))
    render(<JobProfileInsights jobId={154} onChanged={onChanged} />)
    const row = (await screen.findByText(textOf('PC电源多相控制器'))).closest('.job-profile-item') as HTMLElement
    await userEvent.click(Array.from(row.querySelectorAll('button')).find(button => button.textContent === '不对') as HTMLButtonElement)

    expect(await screen.findByText(/已记录"PC电源多相控制器"不对/)).toBeInTheDocument()
    await waitFor(() => expect(profileGets).toBe(2))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/顾问已标记不对（1 条/)).toBeInTheDocument()
    expect(screen.queryByText('画像回读失败')).not.toBeInTheDocument()
    expect(screen.queryByText('岗位详情回读失败')).not.toBeInTheDocument()
  })

  it('加载失败如实呈现错误并可原地重试', async () => {
    let attempts = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => {
      attempts += 1
      return attempts === 1 ? mockResponse({ error: '服务不可用' }, false, 503) : mockResponse(readyPayload)
    }))
    render(<JobProfileInsights jobId={154} />)
    expect(await screen.findByText('服务不可用')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '重新加载岗位画像' }))
    expect(await screen.findByText(/来源 5 份人选履历/)).toBeInTheDocument()
    expect(attempts).toBe(2)
  })
})

describe('岗位详情接线（JobPanel 挂载画像区块）', () => {
  const jobDetail: JobDetail = {
    id: 154,
    title: '技术市场经理/总监（PC电源）',
    client: '士兰微',
    candidate_count: 3,
    active_candidate_count: 3,
    position: {},
    profile: {},
    funnel: { total: 3, active: 3, stopped: 0, contacted: 0, recommended: 0 },
    stages: [],
    candidates: [],
    search_experiments: [],
    events: [],
    followups: [],
  }

  it('岗位详情页渲染「这个岗位实际在干什么」区块', async () => {
    stubFetch((url) => (url.includes('/profile-insights') ? readyPayload : readyPayload))
    render(<JobPanel value={jobDetail} close={() => undefined} openCandidate={() => undefined} />)
    expect(await screen.findByText('这个岗位实际在干什么')).toBeInTheDocument()
    expect(await screen.findByText(/来源 5 份人选履历/)).toBeInTheDocument()
  })

  it('岗位详情把画像纠正成功通知接到父级详情刷新', async () => {
    let disputed = false
    const changed = vi.fn()
    stubFetch((url, init) => {
      if (url.includes('/profile-insights/feedback') && init?.method === 'POST') {
        disputed = true
        return { ok: true, status: 'disputed' }
      }
      return disputed ? disputedPayload : readyPayload
    })
    render(<JobPanel value={jobDetail} close={() => undefined} openCandidate={() => undefined} changed={changed} />)
    const row = (await screen.findByText(textOf('PC电源多相控制器'))).closest('.job-profile-item') as HTMLElement
    await userEvent.click(Array.from(row.querySelectorAll('button')).find(button => button.textContent === '不对') as HTMLButtonElement)
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
  })

  it('数据不足时区块显示空态，不影响其余区块渲染', async () => {
    stubFetch(() => insufficientPayload)
    render(<JobPanel value={jobDetail} close={() => undefined} openCandidate={() => undefined} />)
    expect(await screen.findByText(/履历还太少，学不出画像/)).toBeInTheDocument()
    expect(screen.getByText('岗位概况')).toBeInTheDocument()
  })

  it('当前策略只读取 latest_effective_strategy，历史查询单列为寻访记录', async () => {
    stubFetch(() => insufficientPayload)
    render(<JobPanel value={{
      ...jobDetail,
      search_words: '旧关键词不应冒充当前策略',
      latest_effective_strategy: {
        status: 'waiting_approval',
        plan_version: 2,
        summary: '服务器电源技术市场核心岗',
        company_tiers: [{ tier: 'T1', companies: ['目标公司A'] }],
        level_mapping: { accepted_levels: ['高级经理', '总监'] },
        keyword_groups: [{ group: 'core', terms: ['服务器电源', '技术市场'] }],
        expectation: { fallback_plan: 'T1 不足时扩展同层客户' },
        consultant_constraints: [{ type: 'must', rule: '必须具备三次电源经验' }],
        audit: { workflow_id: 'workflow-new' },
      },
      search_experiments: [{ id: 1, query: '历史查询词', channel: 'liepin', result_count: 12 }],
    }} close={() => undefined} openCandidate={() => undefined} />)

    expect(screen.getByText('当前寻访策略')).toBeInTheDocument()
    expect(screen.getByText(/服务器电源技术市场核心岗 · 计划 v2/)).toBeInTheDocument()
    expect(screen.getByText('必须具备三次电源经验')).toBeInTheDocument()
    expect(screen.queryByText('旧关键词不应冒充当前策略')).not.toBeInTheDocument()
    expect(screen.getByText('寻访记录')).toBeInTheDocument()
    expect(screen.getByText('历史查询词')).toBeInTheDocument()
  })
})
