import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, clearStrategyReviewCache } from '../api'
import { RevisePlanDialog } from '../components/RevisePlanDialog'
import { StrategyReview } from '../workflows/StrategyReview'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

// S4-3 策略复盘：复盘展示、404 空态与生成、revision_diff 逐项采纳/拒绝、决策并入 revise instruction。
// S4-3c：决策经 PATCH /strategy-review/diffs 持久化到后端（事实源为复盘 revision_diff[].status），
// localStorage 降级为 API 失败时的缓存回退；提交 revise 时同步发一次 PATCH（各自幂等）。

const reviewPayload = {
  ok: true,
  artifact_id: 'strategy_review_wf-1',
  workflow_id: 'wf-1',
  title: '没成的原因 v1：策略问题：关键词/目标池太窄',
  content: '# 没成的原因',
  review: {
    verdict: 'strategy_too_narrow',
    verdict_label: '策略问题：关键词/目标池太窄',
    verdict_reason: '本轮总召回 12 < step5 预期总量 40 的 50%（20），判定策略问题：关键词/目标池太窄',
    degraded: false,
    thresholds: { recall_shortfall_ratio: 0.5, detail_failed_ratio: 0.3, high_score_rate: 0.15 },
    evidence: {
      has_strategy_v2: true, funnel_channels: 2, expected_recall_total: 40, recall_total: 12,
      detail_total: 10, detail_failed_total: 1, detail_failed_ratio: 0.1,
      intake_new_total: 8, assessed_total: 6, high_score_total: 2, high_score_rate: 0.3333,
      assessment_source: 'funnel',
    },
    per_channel_findings: [
      {
        channel: 'liepin', status: 'completed', recall_count: 12, unique_count: 10, intake_new_count: 8,
        assessed_count: 6, high_score_count: 2, detail_complete: 9, detail_partial: 0, detail_failed: 1,
        detail_failed_ratio: 0.1, zero_attribution: null, finding: 'ok', note: '渠道各环节未见异常',
      },
      {
        channel: 'xsaas', status: 'completed', recall_count: 0, unique_count: 0, intake_new_count: 0,
        assessed_count: 0, high_score_count: 0, detail_complete: 0, detail_partial: 0, detail_failed: 0,
        detail_failed_ratio: null, zero_attribution: 'session_expired', finding: 'execution_issue',
        note: '0 召回：登录态失效，需重新登录该渠道（执行/渠道类）',
      },
    ],
    revision_diff: [
      {
        diff_id: 'diff-1', step: 'step2_target_pool', op: 'add', tier: 'T2',
        companies: ['甲公司', '乙公司'],
        reason: '召回 12 不及预期 50%（40），按 fallback_plan 放宽目标池：增列 T2 公司',
        status: 'pending',
      },
      {
        diff_id: 'diff-2', step: 'step4_keyword_groups', op: 'replace', group: '核心词',
        terms: ['功率半导体', 'MOSFET'],
        reason: '关键词组“核心词”召回不足，建议替换为更宽的知识库锚定词组',
        status: 'pending',
      },
    ],
    escalation: null,
    notes: ['知识库暂无可增补的目标池公司，step2 修订待顾问补充'],
    generated_at: '2026-07-23 10:00:00',
    version: 1,
    history: [],
  },
}

const insufficientPayload = {
  ...reviewPayload,
  title: '没成的原因 v1：数据不足，不硬判',
  review: {
    ...reviewPayload.review,
    verdict: 'insufficient_data',
    verdict_label: '数据不足，不硬判',
    verdict_reason: '该轮未记录渠道漏斗明细，无法判断是策略问题还是执行问题，不硬下结论',
    degraded: true,
    per_channel_findings: [],
    revision_diff: [],
    notes: ['可得证据（评估表）：评估 6 人、高分 2 人；仅作参考，不足以判断是策略问题还是执行问题'],
  },
}

const decisionsKey = 'asa_strategy_review_diffs:wf-1'
const storedDecisions = () => JSON.parse(window.localStorage.getItem(decisionsKey) || '{}') as Record<string, string>

const stubReviewFetch = (payload: unknown = reviewPayload) => {
  const fetchMock = vi.fn<typeof fetch>(async () => mockResponse(payload))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
  window.sessionStorage.clear()
  clearStrategyReviewCache()
})

describe('策略复盘展示（StrategyReview）', () => {
  it('工作流更新晚于复盘时标记过期，并提供重新分析入口', async () => {
    stubReviewFetch(reviewPayload)
    const user = userEvent.setup()
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="2026-07-23 11:00:00" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    expect(within(section).getByText('复盘已过期')).toBeInTheDocument()
    expect(within(section).getByRole('status')).toHaveTextContent('工作流在这份复盘生成后又发生过更新')
    expect(within(section).getByRole('button', { name: '重新分析' })).toBeInTheDocument()
    await user.click(within(section).getByRole('button', { name: '重新分析' }))
    expect((await screen.findByText('策略问题：关键词/目标池太窄')).textContent).toContain('策略问题')
  })

  it('渲染判定、证据行、渠道简表与修订 diff 列表，并回显本地决策标记', async () => {
    window.localStorage.setItem(decisionsKey, JSON.stringify({ 'diff-1': 'accepted' }))
    stubReviewFetch()
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="2026-07-23 10:05" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    // 判定与理由
    expect(await within(section).findByText('策略问题：关键词/目标池太窄')).toBeInTheDocument()
    expect(within(section).getByText(/本轮总召回 12 < step5 预期总量 40/)).toBeInTheDocument()
    expect(within(section).getByText('v1 · 生成于 2026-07-23 10:00:00')).toBeInTheDocument()
    // 关键证据行：召回/入库/评估/高分
    expect(within(section).getByText(/预期 40/)).toBeInTheDocument()
    expect(within(section).getByText('12')).toBeInTheDocument()
    expect(within(section).getByText(/33%/)).toBeInTheDocument()
    // 渠道简表
    expect(within(section).getByText('猎聘')).toBeInTheDocument()
    expect(within(section).getByText('X-SaaS')).toBeInTheDocument()
    expect(within(section).getByText('召回 12 · 入库 8 · 评估 6 · 高分 2')).toBeInTheDocument()
    expect(within(section).getByText('执行/渠道')).toBeInTheDocument()
    expect(within(section).getByText('0 召回：登录态失效，需重新登录该渠道（执行/渠道类）')).toBeInTheDocument()
    // 修订 diff 列表：step 中文名 + 操作 + 内容 + reason
    expect(within(section).getByText('目标公司池 · 增列')).toBeInTheDocument()
    expect(within(section).getByText('T2：甲公司、乙公司')).toBeInTheDocument()
    expect(within(section).getByText(/按 fallback_plan 放宽目标池/)).toBeInTheDocument()
    expect(within(section).getByText('关键词组 · 替换')).toBeInTheDocument()
    expect(within(section).getByText('「核心词」功率半导体、MOSFET')).toBeInTheDocument()
    // 主面板不再读取浏览器本地决策，复盘只展示后端事实。
    expect(within(section).getAllByText('待决策')).toHaveLength(2)
    expect(within(section).getByText('在 Agent 中讨论并确认应用')).toBeInTheDocument()
    expect(within(section).queryByText('已采纳')).not.toBeInTheDocument()
    expect(within(section).queryByText('已拒绝')).not.toBeInTheDocument()
    // 备注如实呈现
    expect(within(section).getByText('知识库暂无可增补的目标池公司，step2 修订待顾问补充')).toBeInTheDocument()
  })

  it('insufficient_data + degraded 如实呈现，不夸大结论', async () => {
    stubReviewFetch(insufficientPayload)
    render(<StrategyReview workflowId="wf-1" status="completed" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    expect(await within(section).findByText('数据不足，不硬判')).toBeInTheDocument()
    expect(within(section).getByText(/无法判断是策略问题还是执行问题/)).toBeInTheDocument()
    expect(within(section).getByText('证据不完整，结论仅供参考')).toBeInTheDocument()
    expect(within(section).queryByText('修订建议')).not.toBeInTheDocument()
  })

  it('404 显示“这轮还没分析没成的原因”，点击分析按钮调 rebuild 后重新拉取', async () => {
    const user = userEvent.setup()
    const onChanged = vi.fn()
    let gets = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/rebuild')) {
        return mockResponse({ ok: true, workflow_id: 'wf-1', artifact_id: 'strategy_review_wf-1', review: reviewPayload.review })
      }
      gets += 1
      return gets === 1 ? mockResponse({ detail: '该工作流还没生成原因分析：wf-1' }, false, 404) : mockResponse(reviewPayload)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" onChanged={onChanged} />)
    expect(await screen.findByText('这轮还没分析没成的原因。可以基于本轮各渠道的结果和评估情况补一份。')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '分析没成的原因' }))
    const rebuildCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/strategy-review/rebuild'))
    expect(rebuildCall).toBeDefined()
    expect((rebuildCall?.[1] as RequestInit).method).toBe('POST')
    expect((rebuildCall?.[1] as RequestInit).headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/strategy-review/rebuild') })
    expect(JSON.parse(String((rebuildCall?.[1] as RequestInit).body))).toMatchObject({ request_id: expect.stringMatching(/^web_/) })
    // rebuild 成功后重新拉取并展示复盘结论
    expect(await screen.findByText('策略问题：关键词/目标池太窄')).toBeInTheDocument()
    expect(gets).toBe(2)
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
  })

  it('重建成功后直接展示 POST 回执，不等待后台复盘与父级详情回读', async () => {
    const user = userEvent.setup()
    const never = new Promise<Response>(() => undefined)
    const onChanged = vi.fn(() => new Promise<void>(() => undefined))
    const rebuiltPayload = {
      ok: true,
      workflow_id: 'wf-1',
      artifact_id: 'strategy_review_wf-1-v2',
      review: {
        ...reviewPayload.review,
        verdict_label: '重建后的策略判断',
        verdict_reason: '这是 rebuild POST 返回的最新数据库结论',
        generated_at: '2026-07-23 11:30:00',
        version: 2,
      },
    }
    let gets = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/rebuild')) return mockResponse(rebuiltPayload)
      gets += 1
      if (gets === 1) return mockResponse({ detail: '该工作流还没生成原因分析：wf-1' }, false, 404)
      return never
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="2026-07-23 12:00:00" onChanged={onChanged} />)
    await user.click(await screen.findByRole('button', { name: '分析没成的原因' }))

    expect(await screen.findByText('重建后的策略判断')).toBeInTheDocument()
    expect(screen.getByText('这是 rebuild POST 返回的最新数据库结论')).toBeInTheDocument()
    expect(screen.getByText('v2 · 生成于 2026-07-23 11:30:00')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '重新分析' })).toBeEnabled())
    await waitFor(() => expect(gets).toBe(2))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('原因分析加载失败')).not.toBeInTheDocument()
  })

  it('重建后的后台回读失败不遮蔽成功回执', async () => {
    const user = userEvent.setup()
    const onChanged = vi.fn(async () => { throw new Error('parent refresh failed') })
    const rebuiltPayload = {
      ok: true,
      workflow_id: 'wf-1',
      artifact_id: 'strategy_review_wf-1-v2',
      review: {
        ...reviewPayload.review,
        verdict_label: '重建成功且保持可见',
        generated_at: '2026-07-23 11:30:00',
        version: 2,
      },
    }
    let gets = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/rebuild')) return mockResponse(rebuiltPayload)
      gets += 1
      if (gets === 1) return mockResponse({ detail: '该工作流还没生成原因分析：wf-1' }, false, 404)
      return mockResponse({ detail: '详情回读失败' }, false, 500)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" onChanged={onChanged} />)
    await user.click(await screen.findByRole('button', { name: '分析没成的原因' }))

    expect(await screen.findByText('重建成功且保持可见')).toBeInTheDocument()
    await waitFor(() => expect(gets).toBe(2))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('原因分析加载失败')).not.toBeInTheDocument()
    expect(screen.queryByText('详情回读失败')).not.toBeInTheDocument()
    expect(screen.queryByText('分析失败，请重试。')).not.toBeInTheDocument()
  })

  it('重建失败不通知父级刷新', async () => {
    const user = userEvent.setup()
    const onChanged = vi.fn()
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/rebuild')) return mockResponse({ detail: '写入失败' }, false, 500)
      return mockResponse({ detail: '该工作流还没生成原因分析：wf-1' }, false, 404)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" onChanged={onChanged} />)
    await user.click(await screen.findByRole('button', { name: '分析没成的原因' }))

    expect(await screen.findByText('写入失败')).toBeInTheDocument()
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('非终局工作流不渲染复盘区，也不发起请求', () => {
    const fetchMock = stubReviewFetch()
    const { container } = render(<StrategyReview workflowId="wf-1" status="running" updatedAt="" />)
    expect(container.firstChild).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('typed client：strategyReview / rebuildStrategyReview', () => {
  it('404 返回 null，其余错误携带 status 抛出', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ detail: '该工作流还没生成原因分析：wf-1' }, false, 404)))
    await expect(api.strategyReview('wf-1')).resolves.toBeNull()
    clearStrategyReviewCache() // 清除404缓存，确保500请求不被缓存拦截
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ detail: 'boom' }, false, 500)))
    await expect(api.strategyReview('wf-1')).rejects.toMatchObject({ status: 500 })
  })

  it('rebuildStrategyReview 走幂等写入封装（Idempotency-Key + request_id）', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true, workflow_id: 'wf-1', artifact_id: 'a', review: reviewPayload.review }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await api.rebuildStrategyReview('wf-1')
    expect(result.artifact_id).toBe('a')
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/workflows/wf-1/strategy-review/rebuild')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/strategy-review/rebuild') })
    expect(JSON.parse(String(init.body))).toMatchObject({ request_id: expect.stringMatching(/^web_/) })
  })
})

describe('修改计划对话框接 revision_diff（RevisePlanDialog）', () => {
  const openDialog = (onSubmit: (instruction: string) => void) => {
    stubReviewFetch()
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={onSubmit} />)
    return screen.findByLabelText('修订建议')
  }

  it('对话框上部展示 diff 条目：step 中文名 + 操作 + 内容 + reason', async () => {
    const region = await openDialog(vi.fn())
    expect(within(region).getByText('目标公司池 · 增列')).toBeInTheDocument()
    expect(within(region).getByText('T2：甲公司、乙公司')).toBeInTheDocument()
    expect(within(region).getByText(/按 fallback_plan 放宽目标池/)).toBeInTheDocument()
    expect(within(region).getByText('关键词组 · 替换')).toBeInTheDocument()
    expect(within(region).getAllByRole('button', { name: '采纳' })).toHaveLength(2)
    expect(within(region).getAllByRole('button', { name: '拒绝' })).toHaveLength(2)
  })

  it('逐项采纳：建议内容预填进 textarea（可再编辑），决策写入 localStorage；再次点击撤销', async () => {
    const user = userEvent.setup()
    const region = await openDialog(vi.fn())
    await user.click(within(region).getAllByRole('button', { name: '采纳' })[0])
    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveValue('增列目标公司池：T2：甲公司、乙公司')
    expect(within(region).getAllByRole('button', { name: '采纳' })[0]).toHaveAttribute('aria-pressed', 'true')
    expect(storedDecisions()).toEqual({ 'diff-1': 'accepted' })
    // 顾问可继续编辑预填内容
    await user.type(textarea, '，优先甲公司')
    expect(textarea).toHaveValue('增列目标公司池：T2：甲公司、乙公司，优先甲公司')
    // 再次点击同一决策撤销（回到待决策）
    await user.click(within(region).getAllByRole('button', { name: '采纳' })[0])
    expect(storedDecisions()).toEqual({})
  })

  it('逐项拒绝：标记已拒绝并写入 localStorage，不预填 textarea', async () => {
    const user = userEvent.setup()
    const region = await openDialog(vi.fn())
    await user.click(within(region).getAllByRole('button', { name: '拒绝' })[1])
    expect(within(region).getAllByRole('button', { name: '拒绝' })[1]).toHaveAttribute('aria-pressed', 'true')
    expect(within(region).getByText('已拒绝')).toBeInTheDocument()
    expect(storedDecisions()).toEqual({ 'diff-2': 'rejected' })
    expect(screen.getByRole('textbox')).toHaveValue('')
  })

  it('提交时逐项采纳/拒绝清单并入 instruction 尾部', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const region = await openDialog(onSubmit)
    await user.click(within(region).getAllByRole('button', { name: '采纳' })[0])
    await user.click(within(region).getAllByRole('button', { name: '拒绝' })[1])
    const textarea = screen.getByRole('textbox')
    await user.clear(textarea)
    await user.type(textarea, '  放宽目标公司池并替换关键词  ')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith('放宽目标公司池并替换关键词\n【逐项采纳】diff-1 【逐项拒绝】diff-2')
  })

  it('未传 workflowId 时不展示 diff 区块（既有行为不回归）', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const fetchMock = stubReviewFetch()
    render(<RevisePlanDialog onCancel={() => undefined} onSubmit={onSubmit} />)
    expect(screen.queryByLabelText('修订建议')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    await user.type(screen.getByRole('textbox'), '  提高学历门槛 ')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledWith('提高学历门槛')
  })

  it('复盘拉取失败时静默降级，修改计划流程不受影响', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => { throw new Error('network down') }))
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={onSubmit} />)
    expect(screen.queryByLabelText('修订建议')).not.toBeInTheDocument()
    await user.type(screen.getByRole('textbox'), '提高学历门槛')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledWith('提高学历门槛')
  })
})

describe('工作流面板：策略编辑统一交给 Agent', () => {
  it('附着工作流上下文后进入 Agent，保留详情且不发 revise 请求', async () => {
    const user = userEvent.setup()
    const close = vi.fn()
    const contexts: unknown[] = []
    window.addEventListener('asa:open-agent', event => contexts.push((event as CustomEvent).detail), { once: true })
    const fetchMock = vi.fn<typeof fetch>(async () => mockResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={close} reload={vi.fn()} openCandidate={() => undefined} archived={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '在 Agent 中讨论策略' }))
    expect(contexts).toEqual([expect.objectContaining({ type: 'workflow', id: 'wf-1' })])
    expect(close).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: plannedWorkflow.goal.title })).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/v1/workflows/wf-1/revise'))).toBe(false)
  })

  it('策略复盘重建成功后刷新父级工作流详情', async () => {
    const user = userEvent.setup()
    const reload = vi.fn()
    let gets = 0
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/rebuild')) {
        return mockResponse({ ok: true, workflow_id: 'wf-1', artifact_id: 'strategy_review_wf-1', review: reviewPayload.review })
      }
      if (url.includes('/strategy-review')) {
        gets += 1
        return gets === 1 ? mockResponse({ detail: '该工作流还没生成原因分析：wf-1' }, false, 404) : mockResponse(reviewPayload)
      }
      if (url.includes('/candidates')) return mockResponse({ ok: true, items: [], total: 0 })
      return mockResponse({ ok: true })
    }))
    const terminalWorkflow = {
      ...plannedWorkflow,
      workflow: { ...plannedWorkflow.workflow, status: 'blocked' },
      goal: { ...plannedWorkflow.goal, status: 'blocked' },
    }

    render(<WorkflowPanel value={terminalWorkflow} jobs={[]} close={vi.fn()} reload={reload} openCandidate={vi.fn()} archived={vi.fn()} />)
    await user.click(await screen.findByRole('button', { name: '分析没成的原因' }))

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
  })
})

describe('S4-3c 决策后端持久化（PATCH /strategy-review/diffs）', () => {
  it('逐项决策同步 PATCH 落库：PATCH 方法、幂等键与请求体正确', async () => {
    const user = userEvent.setup()
    const fetchMock = stubReviewFetch()
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={vi.fn()} />)
    const region = await screen.findByLabelText('修订建议')
    await user.click(within(region).getAllByRole('button', { name: '采纳' })[0])
    const patchCall = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes('/strategy-review/diffs') && (init as RequestInit | undefined)?.method === 'PATCH',
    )
    expect(patchCall).toBeDefined()
    expect(String(patchCall?.[0])).toBe('/api/v1/workflows/wf-1/strategy-review/diffs')
    const init = patchCall?.[1] as RequestInit
    expect(init.headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/strategy-review/diffs') })
    expect(JSON.parse(String(init.body))).toMatchObject({
      request_id: expect.stringMatching(/^web_/),
      decisions: [{ diff_id: 'diff-1', status: 'accepted' }],
    })
  })

  it('PATCH 失败时回退 localStorage 缓存，交互与决策标记不丢', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/diffs')) return mockResponse({ detail: 'boom' }, false, 500)
      return mockResponse(reviewPayload)
    }))
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={vi.fn()} />)
    const region = await screen.findByLabelText('修订建议')
    await user.click(within(region).getAllByRole('button', { name: '拒绝' })[1])
    // API 失败不阻断：按钮态、标记与本地缓存照常
    expect(within(region).getAllByRole('button', { name: '拒绝' })[1]).toHaveAttribute('aria-pressed', 'true')
    expect(within(region).getByText('已拒绝')).toBeInTheDocument()
    expect(storedDecisions()).toEqual({ 'diff-2': 'rejected' })
  })

  it('复盘已决状态为事实源：打开对话框时后端 status 覆盖本地暂存，未决条目保留本地', async () => {
    window.localStorage.setItem(decisionsKey, JSON.stringify({ 'diff-1': 'accepted', 'diff-2': 'rejected' }))
    const serverPayload = {
      ...reviewPayload,
      review: {
        ...reviewPayload.review,
        revision_diff: reviewPayload.review.revision_diff.map((diff, index) => ({
          ...diff,
          status: index === 0 ? 'rejected' : 'pending',
        })),
      },
    }
    stubReviewFetch(serverPayload)
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={vi.fn()} />)
    const region = await screen.findByLabelText('修订建议')
    // 后端 diff-1=rejected 覆盖本地 accepted；diff-2 后端 pending 保留本地 rejected 暂存
    await waitFor(() => expect(within(region).getAllByRole('button', { name: '拒绝' })[0]).toHaveAttribute('aria-pressed', 'true'))
    expect(within(region).getAllByRole('button', { name: '采纳' })[0]).toHaveAttribute('aria-pressed', 'false')
    expect(within(region).getAllByRole('button', { name: '拒绝' })[1]).toHaveAttribute('aria-pressed', 'true')
    expect(storedDecisions()).toEqual({ 'diff-1': 'rejected', 'diff-2': 'rejected' })
  })

  it('typed client：patchStrategyReviewDiffs 走 PATCH + 幂等键 + request_id', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      mockResponse({ ok: true, workflow_id: 'wf-1', artifact_id: 'strategy_review_wf-1', updated: 1, revision_diff: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await api.patchStrategyReviewDiffs('wf-1', [{ diff_id: 'diff-1', status: 'accepted' }])
    expect(result.updated).toBe(1)
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/workflows/wf-1/strategy-review/diffs')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('PATCH')
    expect(init.headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/strategy-review/diffs') })
    expect(JSON.parse(String(init.body))).toMatchObject({
      request_id: expect.stringMatching(/^web_/),
      decisions: [{ diff_id: 'diff-1', status: 'accepted' }],
    })
  })

  it('工作流面板不再提供本地 revise 入口', () => {
    render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={() => undefined} reload={vi.fn()} openCandidate={() => undefined} archived={() => undefined} />)
    expect(screen.queryByRole('button', { name: '修改计划' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '调整条件再搜' })).not.toBeInTheDocument()
  })
})
