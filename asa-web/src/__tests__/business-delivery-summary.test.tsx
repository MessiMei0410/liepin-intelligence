import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Workflow } from '../api'
import { BusinessDeliverySummary } from '../workflows/BusinessDeliverySummary'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

const base = (overrides: Partial<Workflow> = {}): Workflow => ({
  ...plannedWorkflow,
  ...overrides,
})

const completedWithAssessment = (): Workflow => base({
  business_outcome: 'completed_target_met',
  goal: { ...plannedWorkflow.goal, status: 'completed', business_outcome: 'completed_target_met' },
  workflow: { workflow_id: 'wf-done', status: 'completed', business_outcome: 'completed_target_met' },
  progress: { completed: 3, total: 3, ratio: 1 },
  steps: [
    { id: 1, sequence: 1, business_label: '锁定岗位核验范围', risk_level: 'R0', status: 'completed' },
    { id: 2, sequence: 2, business_label: '整理候选人核验队列', risk_level: 'R1', status: 'completed' },
    {
      id: 3,
      sequence: 3,
      business_label: '生成逐人核验点',
      risk_level: 'R1',
      status: 'completed',
      capability_id: 'candidate_batch_assessment',
      output: { assessment_queue: { completed: 20, started: 5, completed_items: [] }, summary: '' },
    },
  ],
  artifacts: [
    { artifact_id: 'a-1', title: '多渠道寻访策略', artifact_type: 'search_strategy', validation_status: 'passed' },
    { artifact_id: 'a-2', title: '人岗匹配报告', artifact_type: 'matching_report', validation_status: 'passed' },
  ],
})

describe('本轮交付速览（BusinessDeliverySummary）', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('计划态无数据时给出等待文案，不渲染英文状态原形', () => {
    const { container } = render(<BusinessDeliverySummary workflow={base()} />)
    expect(screen.getByRole('region', { name: '本轮交付速览' })).toBeInTheDocument()
    expect(screen.getByText('计划就绪，等待确认')).toBeInTheDocument()
    expect(screen.getByText('当前无风险标记')).toBeInTheDocument()
    expect(screen.getByText('确认计划并准备')).toBeInTheDocument()
    expect(screen.getByText('等待渠道回执')).toBeInTheDocument()
    expect(screen.getByText('未生成产物：计划尚未执行，开始后这里会显示业务产物或结果去向。')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/planned|pending|waiting_approval|waiting_external|business_outcome|target_met|needs_review|pool_insufficient|technical_failed|cancelled|completed|failed|blocked/)
  })

  it('完成态复用 statusMapping 业务文案，展示评估结果与产物清单', () => {
    const { container } = render(<BusinessDeliverySummary workflow={completedWithAssessment()} />)
    expect(screen.getAllByText('本轮完成，达成目标').length).toBeGreaterThan(0)
    expect(screen.getByText('本轮评估 5 位 · 岗位累计已评估 20 人')).toBeInTheDocument()
    expect(screen.getByText('已生成 2 个产物：多渠道寻访策略、人岗匹配报告')).toBeInTheDocument()
    expect(screen.getByText('本轮目标达成，无待处理动作')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/completed|target_met|business_outcome/)
  })

  it('取消态给业务解释：已取消 + 可归档，产物说明沿用状态解释', () => {
    const { container } = render(<BusinessDeliverySummary workflow={base({
      workflow: { workflow_id: 'wf-cancel', status: 'cancelled' },
      steps: [{ id: 1, sequence: 1, business_label: '执行多渠道寻访', risk_level: '中', status: 'cancelled' }],
    })} />)
    expect(screen.getByText('本轮已取消')).toBeInTheDocument()
    expect(screen.getByText('本轮已结束，可归档或重新发起')).toBeInTheDocument()
    expect(screen.getByText('未生成产物：工作流已取消；取消前没有生成可查看产物。')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/cancelled/)
  })

  it('失败态说明失败步骤、风险与重试动作，候选结果如实标注无结果', () => {
    const { container } = render(<BusinessDeliverySummary workflow={base({
      workflow: { workflow_id: 'wf-fail', status: 'failed' },
      goal: { ...plannedWorkflow.goal, status: 'failed' },
      progress: { completed: 1, total: 2, ratio: 0.5 },
      steps: [
        { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed' },
        { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'failed', error: 'Traceback (most recent call last)' },
      ],
    })} />)
    expect(screen.getAllByText('技术失败：执行多渠道寻访').length).toBeGreaterThan(0)
    expect(screen.getByText('中风险：执行多渠道寻访 执行失败')).toBeInTheDocument()
    expect(screen.getByText('重试失败步骤：执行多渠道寻访')).toBeInTheDocument()
    expect(screen.getByText('暂无候选结果')).toBeInTheDocument()
    expect(screen.getByText('未生成产物：工作流未形成可查看产物，请先处理失败或阻塞步骤。')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/failed|traceback/i)
  })

  it('blocked + 合格人数不足 → 瓶颈与下一步都给业务结论，不出英文枚举', () => {
    const { container } = render(<BusinessDeliverySummary workflow={base({
      business_outcome: 'completed_needs_review',
      goal: { ...plannedWorkflow.goal, status: 'blocked', business_outcome: 'completed_needs_review' },
      workflow: { workflow_id: 'wf-review', status: 'blocked', business_outcome: 'completed_needs_review' },
      progress: { completed: 2, total: 2, ratio: 1 },
      steps: [
        { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed' },
        { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'completed' },
      ],
    })} />)
    expect(screen.getAllByText('本轮完成，合格人数不足，有待复核人选').length).toBeGreaterThan(0)
    expect(screen.getByText('复核现有人选或在 Agent 中调整策略')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/completed_needs_review|blocked|needs_review/)
  })

  it('渠道产物有数据时给出漏斗、召回与入库概览', () => {
    const workflow = base({
      workflow: { workflow_id: 'wf-sourcing', status: 'completed' },
      goal: { ...plannedWorkflow.goal, status: 'completed' },
      progress: { completed: 2, total: 2, ratio: 1 },
      steps: [
        { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed' },
        {
          id: 2,
          sequence: 2,
          business_label: '执行多渠道寻访',
          risk_level: '中',
          status: 'completed',
          capability_id: 'multi_channel_sourcing',
          output: {
            diagnosis: { funnel: { total: 12, pending_review: 3 } },
            external_result: {
              channel_runs: [
                { channel: 'liepin', result: { candidates: 5 } },
                { channel: 'xsaas', result: { candidates: 3 } },
              ],
              intake: { applied: { inserted: 2, skipped_existing: 5 } },
            },
          },
        },
      ],
    })
    render(<BusinessDeliverySummary workflow={workflow} />)
    expect(screen.getByText(/人才漏斗 12 人，待复核 3 人/)).toBeInTheDocument()
    expect(screen.getByText(/渠道合计召回 8 条候选/)).toBeInTheDocument()
    expect(screen.getByText(/本轮新增入库 2 人，跳过已有 5 人/)).toBeInTheDocument()
  })

  it('等待外部结果时瓶颈与下一步说明等待渠道，候选结果标等待渠道回执', () => {
    render(<BusinessDeliverySummary workflow={base({
      workflow: { workflow_id: 'wf-wait', status: 'waiting_external' },
      goal: { ...plannedWorkflow.goal, status: 'waiting_external' },
      progress: { completed: 1, total: 2, ratio: 0.5 },
      steps: [
        { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed' },
        { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'waiting_external' },
      ],
    })} />)
    expect(screen.getByText('等待渠道返回：执行多渠道寻访')).toBeInTheDocument()
    expect(screen.getByText('等待渠道返回寻访结果')).toBeInTheDocument()
    expect(screen.getByText('等待渠道回执')).toBeInTheDocument()
  })

  it('接入 WorkflowPanel 后渲染在详情顶部', () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/candidates')
      ? mockResponse({ ok: true, items: [], total: 0 })
      : mockResponse({ ok: true })))
    const { container } = render(<WorkflowPanel value={completedWithAssessment()} jobs={[]} close={() => undefined} reload={vi.fn()} openCandidate={() => undefined} archived={vi.fn()} />)
    expect(screen.getByRole('region', { name: '本轮交付速览' })).toBeInTheDocument()
    const main = container.querySelector('.workflow-body main')
    const summary = main?.querySelector('.workflow-delivery')
    expect(summary).not.toBeNull()
    expect(main?.firstElementChild?.nextElementSibling).toBe(summary)
  })
})


// R10 预计产出行：策略 step5 预期召回（expected_recall_per_tier 求和）+ 寻访目标人数
// （sourcing 步 external_request.target_count，回落审批 preflight.target_count）。
// 计划/执行中显示预期；终态显示实际 vs 预期；无数据如实「未设定预期产出」。
describe('本轮交付速览 · 预计产出（R10）', () => {
  it('执行中有预期：策略预期召回求和 + sourcing 目标人数', () => {
    render(<BusinessDeliverySummary workflow={base({
      workflow: { workflow_id: 'wf-run', status: 'running' },
      goal: { ...plannedWorkflow.goal, status: 'running' },
      progress: { completed: 1, total: 2, ratio: 0.5 },
      steps: [
        {
          id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed',
          capability_id: 'search_strategy',
          output: { strategy_v2: { step5_expectation: { expected_recall_per_tier: { T1: 10, T2: 30 } } } },
        },
        {
          id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'running',
          capability_id: 'multi_channel_sourcing',
          output: { external_request: { target_count: 10 } },
        },
      ],
    })} />)
    expect(screen.getByText('预计召回 40 条候选 · 目标 10 人')).toBeInTheDocument()
  })

  it('无预期数据：如实显示「未设定预期产出」', () => {
    render(<BusinessDeliverySummary workflow={base()} />)
    expect(screen.getByText('预计产出')).toBeInTheDocument()
    expect(screen.getByText('未设定预期产出')).toBeInTheDocument()
  })

  it('目标人数回落审批 preflight.target_count（sourcing 步无 output 时）', () => {
    render(<BusinessDeliverySummary workflow={base({
      approvals: [{
        approval_id: 'ap-1', title: '多渠道寻访审批', risk_level: '中', status: 'approved', created_at: '2026-07-24 10:00:00',
        preflight: { target_count: 15 },
      }],
      steps: [
        { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'pending' },
        { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'pending', capability_id: 'multi_channel_sourcing' },
      ],
    })} />)
    expect(screen.getByText('目标 15 人')).toBeInTheDocument()
  })

  it('终态：预计产出行显示实际 vs 预期对比', () => {
    render(<BusinessDeliverySummary workflow={base({
      business_outcome: 'completed_target_met',
      goal: { ...plannedWorkflow.goal, status: 'completed', business_outcome: 'completed_target_met' },
      workflow: { workflow_id: 'wf-exp', status: 'completed', business_outcome: 'completed_target_met' },
      progress: { completed: 2, total: 2, ratio: 1 },
      steps: [
        {
          id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'completed',
          capability_id: 'search_strategy',
          output: { strategy_v2: { step5_expectation: { expected_recall_per_tier: { T1: 10, T2: 30 } } } },
        },
        {
          id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'completed',
          capability_id: 'multi_channel_sourcing',
          output: {
            external_request: { target_count: 10 },
            external_result: {
              channel_runs: [
                { channel: 'liepin', result: { candidates: 5 } },
                { channel: 'xsaas', result: { candidates: 3 } },
              ],
              intake: { applied: { inserted: 2, skipped_existing: 5 } },
            },
          },
        },
      ],
    })} />)
    expect(screen.getByText('实际召回 8 条（预期 40） · 实际入库 2 人（目标 10）')).toBeInTheDocument()
  })
})
