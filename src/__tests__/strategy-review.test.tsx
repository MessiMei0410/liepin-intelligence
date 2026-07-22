import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api } from '../api'
import { RevisePlanDialog } from '../components/RevisePlanDialog'
import { StrategyReview } from '../workflows/StrategyReview'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

// S4-3 策略复盘：复盘展示、404 空态与生成、revision_diff 逐项采纳/拒绝、决策并入 revise instruction。
// 后端本期无条目级 status 回写接口，决策暂存 localStorage（键 asa_strategy_review_diffs:{workflow_id}）。

const reviewPayload = {
  ok: true,
  artifact_id: 'strategy_review_wf-1',
  workflow_id: 'wf-1',
  title: '策略复盘 v1：策略问题：关键词/目标池太窄',
  content: '# 策略复盘',
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
        note: '0 召回归因 session_expired（执行/渠道类）',
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
  title: '策略复盘 v1：数据不足，不硬判',
  review: {
    ...reviewPayload.review,
    verdict: 'insufficient_data',
    verdict_label: '数据不足，不硬判',
    verdict_reason: '该轮未记录渠道漏斗明细，无法判定策略/执行归因，不硬判',
    degraded: true,
    per_channel_findings: [],
    revision_diff: [],
    notes: ['可得证据（评估表）：评估 6 人、高分 2 人；仅作参考，不足以支撑策略/执行归因'],
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
})

describe('策略复盘展示（StrategyReview）', () => {
  it('渲染判定、证据行、渠道简表与修订 diff 列表，并回显本地决策标记', async () => {
    window.localStorage.setItem(decisionsKey, JSON.stringify({ 'diff-1': 'accepted' }))
    stubReviewFetch()
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="2026-07-23 10:05" />)
    const section = await screen.findByRole('region', { name: '策略复盘' })
    // 判定与理由
    expect(within(section).getByText('策略问题：关键词/目标池太窄')).toBeInTheDocument()
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
    expect(within(section).getByText('0 召回归因 session_expired（执行/渠道类）')).toBeInTheDocument()
    // 修订 diff 列表：step 中文名 + 操作 + 内容 + reason
    expect(within(section).getByText('目标公司池 · 增列')).toBeInTheDocument()
    expect(within(section).getByText('T2：甲公司、乙公司')).toBeInTheDocument()
    expect(within(section).getByText(/按 fallback_plan 放宽目标池/)).toBeInTheDocument()
    expect(within(section).getByText('关键词组 · 替换')).toBeInTheDocument()
    expect(within(section).getByText('「核心词」功率半导体、MOSFET')).toBeInTheDocument()
    // 决策标记：diff-1 本地已采纳，diff-2 待决策
    expect(within(section).getByText('已采纳')).toBeInTheDocument()
    expect(within(section).getAllByText('待决策')).toHaveLength(1)
    expect(within(section).queryByText('已拒绝')).not.toBeInTheDocument()
    // 备注如实呈现
    expect(within(section).getByText('知识库暂无可增补的目标池公司，step2 修订待顾问补充')).toBeInTheDocument()
  })

  it('insufficient_data + degraded 如实呈现，不夸大结论', async () => {
    stubReviewFetch(insufficientPayload)
    render(<StrategyReview workflowId="wf-1" status="completed" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '策略复盘' })
    expect(within(section).getByText('数据不足，不硬判')).toBeInTheDocument()
    expect(within(section).getByText(/无法判定策略\/执行归因，不硬判/)).toBeInTheDocument()
    expect(within(section).getByText('证据不完整，结论仅供参考')).toBeInTheDocument()
    expect(within(section).queryByText('修订建议')).not.toBeInTheDocument()
  })

  it('404 显示“该轮未生成策略复盘”，点击生成复盘调 rebuild 后重新拉取', async () => {
    const user = userEvent.setup()
    let gets = 0
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review/rebuild')) {
        return mockResponse({ ok: true, workflow_id: 'wf-1', artifact_id: 'strategy_review_wf-1', review: reviewPayload.review })
      }
      gets += 1
      return gets === 1 ? mockResponse({ detail: '该工作流暂无策略复盘：wf-1' }, false, 404) : mockResponse(reviewPayload)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    expect(await screen.findByText('该轮未生成策略复盘。可基于本轮渠道漏斗与评估结果补生成。')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '生成复盘' }))
    const rebuildCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/strategy-review/rebuild'))
    expect(rebuildCall).toBeDefined()
    expect((rebuildCall?.[1] as RequestInit).method).toBe('POST')
    expect((rebuildCall?.[1] as RequestInit).headers).toMatchObject({ 'Idempotency-Key': expect.stringContaining('/strategy-review/rebuild') })
    expect(JSON.parse(String((rebuildCall?.[1] as RequestInit).body))).toMatchObject({ request_id: expect.stringMatching(/^web_/) })
    // rebuild 成功后重新拉取并展示复盘结论
    expect(await screen.findByText('策略问题：关键词/目标池太窄')).toBeInTheDocument()
    expect(gets).toBe(2)
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
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => mockResponse({ detail: '该工作流暂无策略复盘：wf-1' }, false, 404)))
    await expect(api.strategyReview('wf-1')).resolves.toBeNull()
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
    return screen.findByLabelText('策略复盘修订建议')
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
    expect(screen.queryByLabelText('策略复盘修订建议')).not.toBeInTheDocument()
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
    expect(screen.queryByLabelText('策略复盘修订建议')).not.toBeInTheDocument()
    await user.type(screen.getByRole('textbox'), '提高学历门槛')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledWith('提高学历门槛')
  })
})

describe('工作流面板：调整条件再搜 → diff 采纳 → revise 提交链路', () => {
  it('采纳预填后提交，revise 请求体带预填内容与逐项决策后缀', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/strategy-review')) return mockResponse(reviewPayload)
      return mockResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={() => undefined} reload={vi.fn()} openCandidate={() => undefined} archived={() => undefined} />)
    await user.click(screen.getByRole('button', { name: '修改计划' }))
    const dialog = await screen.findByRole('dialog')
    const region = await within(dialog).findByLabelText('策略复盘修订建议')
    await user.click(within(region).getAllByRole('button', { name: '采纳' })[0])
    await user.click(within(dialog).getByRole('button', { name: '确认修改' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    const reviseCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/v1/workflows/wf-1/revise'))
    expect(reviseCall).toBeDefined()
    expect(JSON.parse(String((reviseCall?.[1] as RequestInit).body))).toMatchObject({
      instruction: '增列目标公司池：T2：甲公司、乙公司\n【逐项采纳】diff-1',
    })
  })
})
