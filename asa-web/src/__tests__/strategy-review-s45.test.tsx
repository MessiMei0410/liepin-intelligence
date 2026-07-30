import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearStrategyReviewCache } from '../api'
import { StrategyReview } from '../workflows/StrategyReview'
import { mockResponse } from './helpers'

// S4-5：复盘卡渲染 N4 渠道降权建议（channel_downweights，仅建议不执行）与
// N5 评估尺度复核（evaluation_review：遮罩名单 + 评分证据链摘要 + "尺度复核"提示/跳转）。
// 候选人姓名一律遮罩呈现，restricted 字面量不出现。

const reviewPayload = {
  ok: true,
  artifact_id: 'strategy_review_wf-1',
  workflow_id: 'wf-1',
  title: '没成的原因 v1：高分率偏低',
  content: '# 没成的原因',
  review: {
    verdict: 'quality_gap',
    verdict_label: '高分率偏低：画像偏差（策略）或评分偏差（评估）',
    verdict_reason: '入库/评估正常（入库 20、评估 5），但高分率 0/5=0% < 阈值 15%；疑似画像偏差或评分偏差',
    degraded: false,
    thresholds: { recall_shortfall_ratio: 0.5, detail_failed_ratio: 0.3, high_score_rate: 0.15 },
    evidence: {
      has_strategy_v2: true, funnel_channels: 1, expected_recall_total: 40, recall_total: 40,
      detail_total: 20, detail_failed_total: 0, detail_failed_ratio: 0,
      intake_new_total: 20, assessed_total: 5, high_score_total: 0, high_score_rate: 0,
      assessment_source: 'funnel',
    },
    per_channel_findings: [
      {
        channel: 'liepin', status: 'completed', recall_count: 40, unique_count: 20, intake_new_count: 20,
        assessed_count: 5, high_score_count: 0, detail_complete: 20, detail_partial: 0, detail_failed: 0,
        detail_failed_ratio: 0, zero_attribution: null, finding: 'low_high_rate', note: '渠道高分率 0/5 低于阈值 15%',
      },
    ],
    revision_diff: [],
    escalation: { kind: 'evaluation_issue_ticket', target: 'evaluation', reason: '入库正常但高分率 0/5 低于阈值 15%，可能为评分偏差', status: 'open' },
    channel_downweights: [
      {
        channel: 'xsaas', archetype_id: 'tme_computing_power', streak: 2, rounds: 2, recall_total: 0,
        reason: 'xsaas×tme_computing_power 连续 2 轮 0 召回且不是渠道故障（累计 2 轮、总召回 0）',
        recommendation: '建议 xsaas 在原型 tme_computing_power 下查询配额降权（如下轮 queries 减半），配额让给高效渠道；执行待顾问确认',
      },
    ],
    evaluation_review: {
      kind: 'evaluation_review',
      prompt: '是尺严还是人不行',
      assessed_total: 5,
      high_score_total: 0,
      items: [
        {
          job_candidate_id: 301, assessment_id: 401, candidate: '李**', company: '下游X公司', title: '机械工程师',
          fit_score: 72, fit_level: 'B-可推进', recommendation: 'verify_first',
          deductions: [
            { group: 'hard_requirements', criterion: '7年以上精密设备机械设计经验', status: 'not_met', critical: true, reason: '年限不足', evidence: ['仅4年相关经验'] },
            { group: 'core_abilities', criterion: '有限元', status: 'not_met', critical: false, reason: '技能缺失', evidence: ['履历未提及有限元'] },
          ],
        },
        {
          job_candidate_id: 302, assessment_id: 402, candidate: '韩**', company: 'ASM中国集团公司', title: '设备工程师',
          fit_score: 68, fit_level: 'C-需确认', recommendation: 'verify_first',
          deductions: [],
        },
      ],
      note: '复核结论请在被否人选详情页以「评分复核」记录，写回候选人事件供后续回看核对',
    },
    notes: [],
    generated_at: '2026-07-23 10:00:00',
    version: 1,
    history: [],
  },
}

const stubReviewFetch = (payload: unknown = reviewPayload) => {
  const fetchMock = vi.fn<typeof fetch>(async () => mockResponse(payload))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
  clearStrategyReviewCache()
})

describe('S4-5 复盘卡：N4 渠道降权建议', () => {
  it('渲染降权建议：渠道×原型、连续轮次、原因与仅建议不执行提示', async () => {
    stubReviewFetch()
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="2026-07-23 10:05" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    const block = await within(section).findByLabelText('渠道降权建议')
    expect(within(block).getByText(/连续 0 召回（非渠道故障）≥2 轮 · 仅建议不执行/)).toBeInTheDocument()
    expect(within(block).getByText('X-SaaS × tme_computing_power')).toBeInTheDocument()
    expect(within(block).getByText('连续 2 轮 0 召回')).toBeInTheDocument()
    expect(within(block).getByText(/不是渠道故障（累计 2 轮、总召回 0）/)).toBeInTheDocument()
    expect(within(block).getByText(/queries 减半/)).toBeInTheDocument()
    // 头部"仅建议不执行"提示与条目建议各出现一次
    expect(within(block).getAllByText(/待顾问确认/)).toHaveLength(2)
  })

  it('无降权（空数组）不渲染降权区', async () => {
    stubReviewFetch({ ...reviewPayload, review: { ...reviewPayload.review, channel_downweights: [] } })
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    await within(section).findByText('高分率偏低：画像偏差（策略）或评分偏差（评估）')
    expect(within(section).queryByLabelText('渠道降权建议')).not.toBeInTheDocument()
  })
})

describe('S4-5 复盘卡：N5 评估尺度复核', () => {
  it('渲染被否人选名单与评分证据链摘要，并附"尺度复核"提示', async () => {
    stubReviewFetch()
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="2026-07-23 10:05" openCandidate={() => undefined} />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    const block = await within(section).findByLabelText('评估尺度复核')
    // 头部：评估数 · 高分 0 · prompt
    expect(within(block).getByText(/评估 5 人 · 高分 0 · 是尺严还是人不行/)).toBeInTheDocument()
    // 名单：遮罩名 + 公司职位 + fit_score
    expect(within(block).getByText('李**')).toBeInTheDocument()
    expect(within(block).getByText('下游X公司 · 机械工程师')).toBeInTheDocument()
    expect(within(block).getByText('fit 72 · B-可推进')).toBeInTheDocument()
    expect(within(block).getByText('韩**')).toBeInTheDocument()
    // 证据链摘要：硬伤在前 + 证据原文
    expect(within(block).getByText(/硬伤：7年以上精密设备机械设计经验（年限不足）——仅4年相关经验/)).toBeInTheDocument()
    expect(within(block).getByText(/扣分：有限元（技能缺失）——履历未提及有限元/)).toBeInTheDocument()
    // 无扣分证据明细的候选人如实降级
    expect(within(block).getByText('无扣分证据明细，建议打开详情核对评分依据。')).toBeInTheDocument()
    // 尺度复核提示（顾问结论回写指引）
    expect(within(block).getByText(/复核结论请在被否人选详情页以「评分复核」记录/)).toBeInTheDocument()
    expect(within(block).getAllByRole('button', { name: /尺度复核/ })).toHaveLength(2)
    // 遮罩边界：明文姓名不出现
    expect(within(block).queryByText(/李雷|韩梅梅/)).not.toBeInTheDocument()
  })

  it('点击"尺度复核"跳转候选人详情页（openCandidate 收到 job_candidate_id）', async () => {
    const user = userEvent.setup()
    const openCandidate = vi.fn()
    stubReviewFetch()
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" openCandidate={openCandidate} />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    const block = await within(section).findByLabelText('评估尺度复核')
    await user.click(within(block).getAllByRole('button', { name: /尺度复核/ })[0])
    expect(openCandidate).toHaveBeenCalledTimes(1)
    expect(openCandidate).toHaveBeenCalledWith(301)
  })

  it('取不到证据链（items 为空）时如实降级提示', async () => {
    stubReviewFetch({
      ...reviewPayload,
      review: {
        ...reviewPayload.review,
        evaluation_review: { kind: 'evaluation_review', prompt: '是尺严还是人不行', assessed_total: 5, high_score_total: 0, items: [] },
      },
    })
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    const block = await within(section).findByLabelText('评估尺度复核')
    expect(within(block).getByText('未取到被否人选评分证据链，请在候选人列表人工抽查。')).toBeInTheDocument()
  })

  it('未触发（evaluation_review 为 null）不渲染复核区', async () => {
    stubReviewFetch({ ...reviewPayload, review: { ...reviewPayload.review, evaluation_review: null } })
    render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    await within(section).findByText('高分率偏低：画像偏差（策略）或评分偏差（评估）')
    expect(within(section).queryByLabelText('评估尺度复核')).not.toBeInTheDocument()
  })
})
