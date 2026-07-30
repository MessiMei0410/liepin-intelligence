import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearStrategyReviewCache } from '../api'
import { RevisePlanDialog } from '../components/RevisePlanDialog'
import { StrategyReview } from '../workflows/StrategyReview'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

// S4-3c-3（N3）池枯竭信号 + 扩池决策树：复盘卡扩区渲染（signals + 按 order 编号步骤列表 + params 摘要，
// 缺省项如实"待顾问补充"）；“调整条件再搜”对话框内逐项采纳/拒绝——树本期无后端 status 回写接口，
// 决策只走 localStorage（键含 workflow_id，条目按 step_id 索引），采纳预填 textarea，提交并入
// "【采纳步骤】…【拒绝步骤】…" 后缀（与 diff 逐项后缀并存）。无树（旧复盘）不渲染新区块。

const reviewPayload = {
  ok: true,
  artifact_id: 'strategy_review_wf-1',
  workflow_id: 'wf-1',
  title: '没成的原因 v1：策略问题：关键词/目标池太窄',
  content: '# 没成的原因',
  review: {
    verdict: 'strategy_too_narrow',
    verdict_label: '策略问题：关键词/目标池太窄',
    verdict_reason: '本轮总召回 120；本轮重复率 99%（119/120） 超过 80% 警戒线，本地人才库基本找遍了，已给出扩圈建议（与上面的结论互不影响）',
    degraded: false,
    signals: [
      {
        signal: 'pool_saturated',
        label: '本地人才库快搜完了（重复率太高）',
        scope: 'round',
        dedupe_rate: 0.9917,
        dedupe_count: 119,
        extracted_count: 120,
        threshold: 0.8,
        channels: [{ channel: 'liepin', extracted_count: 120, dedupe_count: 119, dedupe_rate: 0.9917 }],
        detail: '本轮抓取 120 条，其中 119 条与库内已有重复（重复率 99.2%，超过 80% 警戒线）：本地人才库基本找遍了，要换打法而不是原样重搜',
        semantics: '轮次级复盘信号（>80%，可配置）；区别于渠道级 0 归因 zero_attribution=pool_saturated（>90%）',
      },
    ],
    expansion_decision_tree: [
      {
        step_id: 'exp-1', order: 1, action_type: 'swap_keywords',
        title: '换关键词组（同池不同词）',
        detail: '排重率 99%（119/120）> 阈值 80%：当前词组覆盖的人选已见完。当前 1 组、候选 0 组，逐组替换重搜。',
        params: {
          current_groups: [{ group: '核心词', targets: '功率半导体', terms: ['功率半导体', 'MOSFET'] }],
          candidate_groups: [],
          rotation: '技术词↔公司词↔职能词（同池不同词）',
        },
        status: 'pending',
      },
      {
        step_id: 'exp-2', order: 2, action_type: 'expand_pool',
        title: '扩池：向 T2（客户整机厂）扩展',
        detail: '按 T1→T2（客户整机厂）→T3（相邻池）顺序扩池。当前已用层：T1；下一层 T2（客户整机厂），可新增公司 2 家。',
        params: {
          current_tiers: ['T1'], next_tier: 'T2', tier_label: 'T2（客户整机厂）',
          companies: ['立讯精密', '工业富联'],
          rationale: '客户整机厂设有同款技术市场职能',
          source_archetype: 'silan_tme（seed_silan_tme_v1.json）',
        },
        status: 'pending',
      },
      {
        step_id: 'exp-3', order: 3, action_type: 'relax_condition',
        title: '放宽条件（年限/职级/地点，逐项记录代价）',
        detail: '逐项放宽准入条件并记录代价，顾问逐项确认。边界：只放宽年限/职级/地点，负向规则不放宽。',
        params: {
          items: [
            { field: '年限', current: null, proposal: '', cost: '', source: 'none', note: '策略里没记录年限门槛，当前值取不到；放宽幅度由顾问定' },
            { field: '职级', current: ['经理', '高级经理'], proposal: '在 accepted_levels 基础上放宽一档（纳入相邻职级）', cost: '层级偏低人选增多，定档口径须同步复核', source: 'step3_level_mapping', note: '' },
            { field: '地点', current: '杭州优先', proposal: '从地点优先策略放宽为全国/周边城市', cost: '人选迁移意愿与到岗率下降', source: 'archetype.location_policy', note: '' },
          ],
          boundary: '不涉及禁挖名单/竞业等 restricted 约束，负向规则不放宽',
        },
        status: 'pending',
      },
      {
        step_id: 'exp-4', order: 4, action_type: 'rebalance_channel',
        title: '渠道再平衡（向高效渠道倾斜）',
        detail: '按本轮漏斗转化率倾斜查询配额：猎聘 入库/去重 8/10（80%）。建议向 liepin 倾斜。',
        params: {
          channel_stats: [
            { channel: 'liepin', recall_count: 120, unique_count: 10, intake_new_count: 8, intake_conversion: 0.8 },
            { channel: 'xsaas', recall_count: 0, unique_count: 0, intake_new_count: 0, intake_conversion: null },
          ],
          recommended_channel: 'liepin',
          basis: 'intake_new_count/unique_count（本轮漏斗转化率）',
        },
        status: 'pending',
      },
      {
        step_id: 'exp-5', order: 5, action_type: 'escalate_mapping',
        title: '转 Mapping 直挖 / 与客户确认方向（升级项）',
        detail: '本地池与渠道池均已尽，继续原样重搜无增量。本步为升级项，须顾问决策后执行。',
        params: { actions: ['mapping_direct_sourcing', 'client_direction_calibration'], reason: '排重率 99%（119/120）> 阈值 80%，本地池+渠道池已尽' },
        status: 'pending',
      },
    ],
    revision_diff: [
      {
        diff_id: 'diff-1', step: 'step2_target_pool', op: 'add', tier: 'T2',
        companies: ['甲公司', '乙公司'],
        reason: '召回不及预期，按 fallback_plan 放宽目标池：增列 T2 公司',
        status: 'pending',
      },
    ],
    escalation: null,
    notes: ['换词步：知识库原型无更多候选关键词组，候选组留空，待顾问补充'],
    generated_at: '2026-07-23 10:00:00',
    version: 1,
    history: [],
  },
}

// 旧复盘（S4-3c-3 之前生成）：无 signals / expansion_decision_tree 字段。
const legacyPayload = {
  ok: true,
  artifact_id: 'strategy_review_wf-1',
  workflow_id: 'wf-1',
  title: '没成的原因 v1：策略问题：关键词/目标池太窄',
  content: '# 没成的原因',
  review: {
    verdict: 'strategy_too_narrow',
    verdict_label: '策略问题：关键词/目标池太窄',
    verdict_reason: '本轮总召回 12 < step5 预期总量 40 的 50%（20）',
    degraded: false,
    revision_diff: [
      {
        diff_id: 'diff-1', step: 'step2_target_pool', op: 'add', tier: 'T2',
        companies: ['甲公司', '乙公司'],
        reason: '召回不及预期，按 fallback_plan 放宽目标池：增列 T2 公司',
        status: 'pending',
      },
    ],
    escalation: null,
    notes: [],
    generated_at: '2026-07-23 09:00:00',
    version: 1,
    history: [],
  },
}

const treeKey = 'asa_strategy_expansion_tree:wf-1'
const storedTreeDecisions = () => JSON.parse(window.localStorage.getItem(treeKey) || '{}') as Record<string, string>

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

describe('复盘卡扩区：池枯竭信号 + 扩池决策树（StrategyExpansionTree）', () => {
  it('渲染信号 tag 与 detail；决策树按 order 渲染 5 步全字段与 params 摘要，缺省项如实"待顾问补充"', async () => {
    stubReviewFetch()
    const { container } = render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    // 信号：池枯竭 tag + detail 一行
    expect(within(section).getByText('本地人才库快搜完了（重复率太高）')).toBeInTheDocument()
    expect(within(section).getByText(/本轮抓取 120 条，其中 119 条与库内已有重复（重复率 99.2%，超过 80% 警戒线）/)).toBeInTheDocument()
    // 决策树头与 5 步编号 + action_type 中文名 + title
    expect(within(section).getByText('人不够时的扩圈建议')).toBeInTheDocument()
    expect(within(section).getByText('1. 换关键词组')).toBeInTheDocument()
    expect(within(section).getByText('换关键词组（同池不同词）')).toBeInTheDocument()
    expect(within(section).getByText('2. 扩池')).toBeInTheDocument()
    expect(within(section).getByText('扩池：向 T2（客户整机厂）扩展')).toBeInTheDocument()
    expect(within(section).getByText('3. 放宽条件')).toBeInTheDocument()
    expect(within(section).getByText('4. 渠道再平衡')).toBeInTheDocument()
    expect(within(section).getByText('5. 转 Mapping 直挖')).toBeInTheDocument()
    // params 摘要：关键词组 / 公司列表 / 放宽项逐项 / 渠道转化 / 升级路径
    expect(within(section).getByText('当前词组：「核心词」功率半导体、MOSFET')).toBeInTheDocument()
    expect(within(section).getByText('候选词组：待顾问补充')).toBeInTheDocument()
    expect(within(section).getByText('扩向 T2（客户整机厂）：立讯精密、工业富联')).toBeInTheDocument()
    expect(within(section).getByText('依据：客户整机厂设有同款技术市场职能')).toBeInTheDocument()
    expect(within(section).getByText('年限：待顾问补充 → 待顾问补充；策略里没记录年限门槛，当前值取不到；放宽幅度由顾问定')).toBeInTheDocument()
    expect(within(section).getByText('职级：经理、高级经理 → 在 accepted_levels 基础上放宽一档（纳入相邻职级）；代价：层级偏低人选增多，定档口径须同步复核')).toBeInTheDocument()
    expect(within(section).getByText('地点：杭州优先 → 从地点优先策略放宽为全国/周边城市；代价：人选迁移意愿与到岗率下降')).toBeInTheDocument()
    expect(within(section).getByText('渠道转化：猎聘 入库/去重 8/10（80%）；X-SaaS 入库/去重 0/0')).toBeInTheDocument()
    expect(within(section).getByText('建议倾斜：猎聘')).toBeInTheDocument()
    expect(within(section).getByText('升级路径：Mapping 直挖、与客户确认方向')).toBeInTheDocument()
    // 决策标记：5 步 + 1 条 diff 全部待决策
    expect(within(section).getAllByText('待决策')).toHaveLength(6)
    // 按 order 渲染（编号步骤列表）
    const steps = container.querySelectorAll('.review-tree-step')
    expect(steps).toHaveLength(5)
    expect(steps[0].textContent).toContain('1. 换关键词组')
    expect(steps[4].textContent).toContain('5. 转 Mapping 直挖')
  })

  it('树乱序到达时仍按 order 升序渲染', async () => {
    const shuffled = {
      ...reviewPayload,
      review: {
        ...reviewPayload.review,
        expansion_decision_tree: [
          reviewPayload.review.expansion_decision_tree[2],
          reviewPayload.review.expansion_decision_tree[0],
          reviewPayload.review.expansion_decision_tree[4],
          reviewPayload.review.expansion_decision_tree[1],
          reviewPayload.review.expansion_decision_tree[3],
        ],
      },
    }
    stubReviewFetch(shuffled)
    const { container } = render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    await screen.findByRole('region', { name: '没成的原因' })
    const steps = container.querySelectorAll('.review-tree-step')
    expect(steps).toHaveLength(5)
    expect(steps[0].textContent).toContain('1. 换关键词组')
    expect(steps[1].textContent).toContain('2. 扩池')
    expect(steps[4].textContent).toContain('5. 转 Mapping 直挖')
  })

  it('忽略本地树决策，只展示后端回传的待决策状态', async () => {
    window.localStorage.setItem(treeKey, JSON.stringify({ 'exp-2': 'accepted', 'exp-5': 'rejected' }))
    stubReviewFetch()
    const { container } = render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    await screen.findByRole('region', { name: '没成的原因' })
    const tree = container.querySelector('.review-tree') as HTMLElement
    expect(within(tree).queryByText('已采纳')).not.toBeInTheDocument()
    expect(within(tree).queryByText('已拒绝')).not.toBeInTheDocument()
    expect(within(tree).getAllByText('待决策')).toHaveLength(5)
    expect(within(tree).getByText(/在 Copilot 中讨论并确认应用/)).toBeInTheDocument()
  })

  it('无信号无树的旧复盘不渲染新区块', async () => {
    stubReviewFetch(legacyPayload)
    const { container } = render(<StrategyReview workflowId="wf-1" status="blocked" updatedAt="" />)
    const section = await screen.findByRole('region', { name: '没成的原因' })
    expect(within(section).getByText('策略问题：关键词/目标池太窄')).toBeInTheDocument()
    expect(within(section).queryByText('人不够时的扩圈建议')).not.toBeInTheDocument()
    expect(within(section).queryByText('本地人才库快搜完了（重复率太高）')).not.toBeInTheDocument()
    expect(container.querySelector('.review-signals')).toBeNull()
    expect(container.querySelector('.review-tree')).toBeNull()
    // 既有 diff 列表照常渲染
    expect(within(section).getByText('目标公司池 · 增列')).toBeInTheDocument()
  })
})

describe('修改计划对话框接扩池决策树（RevisePlanDialog）', () => {
  const openDialog = (onSubmit: (instruction: string) => void) => {
    const fetchMock = stubReviewFetch()
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={onSubmit} />)
    return { fetchMock, region: screen.findByLabelText('人不够时的扩圈建议') }
  }

  it('对话框内渲染决策树 5 步（编号 + 中文名 + title + params 摘要），每步带采纳/拒绝按钮；diff 区块并存', async () => {
    const { region } = openDialog(vi.fn())
    const tree = await region
    expect(within(tree).getByText('1. 换关键词组')).toBeInTheDocument()
    expect(within(tree).getByText('2. 扩池')).toBeInTheDocument()
    expect(within(tree).getByText('3. 放宽条件')).toBeInTheDocument()
    expect(within(tree).getByText('4. 渠道再平衡')).toBeInTheDocument()
    expect(within(tree).getByText('5. 转 Mapping 直挖')).toBeInTheDocument()
    expect(within(tree).getByText('候选词组：待顾问补充')).toBeInTheDocument()
    expect(within(tree).getByText('扩向 T2（客户整机厂）：立讯精密、工业富联')).toBeInTheDocument()
    expect(within(tree).getAllByRole('button', { name: '采纳' })).toHaveLength(5)
    expect(within(tree).getAllByRole('button', { name: '拒绝' })).toHaveLength(5)
    // 既有 diff 区块并存不回归
    expect(screen.getByLabelText('修订建议')).toBeInTheDocument()
  })

  it('逐项采纳：params 摘要预填 textarea，决策写入 localStorage，不发 PATCH；再次点击撤销', async () => {
    const user = userEvent.setup()
    const { fetchMock, region } = openDialog(vi.fn())
    const tree = await region
    await user.click(within(tree).getAllByRole('button', { name: '采纳' })[1])
    expect(screen.getByRole('textbox')).toHaveValue(
      '扩池：向 T2（客户整机厂）扩展\n扩向 T2（客户整机厂）：立讯精密、工业富联\n依据：客户整机厂设有同款技术市场职能',
    )
    expect(within(tree).getAllByRole('button', { name: '采纳' })[1]).toHaveAttribute('aria-pressed', 'true')
    expect(within(tree).getByText('已采纳')).toBeInTheDocument()
    expect(storedTreeDecisions()).toEqual({ 'exp-2': 'accepted' })
    // 树本期无后端回写接口：全程不得出现 /strategy-review/diffs 的 PATCH
    expect(fetchMock.mock.calls.find(([input]) => String(input).includes('/strategy-review/diffs'))).toBeUndefined()
    // 再次点击同一决策撤销（回到待决策）
    await user.click(within(tree).getAllByRole('button', { name: '采纳' })[1])
    expect(storedTreeDecisions()).toEqual({})
    expect(within(tree).getAllByRole('button', { name: '采纳' })[1]).toHaveAttribute('aria-pressed', 'false')
  })

  it('逐项拒绝：标记已拒绝并写入 localStorage，不预填 textarea', async () => {
    const user = userEvent.setup()
    const { region } = openDialog(vi.fn())
    const tree = await region
    await user.click(within(tree).getAllByRole('button', { name: '拒绝' })[0])
    expect(within(tree).getAllByRole('button', { name: '拒绝' })[0]).toHaveAttribute('aria-pressed', 'true')
    expect(within(tree).getByText('已拒绝')).toBeInTheDocument()
    expect(storedTreeDecisions()).toEqual({ 'exp-1': 'rejected' })
    expect(screen.getByRole('textbox')).toHaveValue('')
  })

  it('提交时树决策与 diff 决策清单一并并入 instruction 尾部', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const { region } = openDialog(onSubmit)
    const tree = await region
    const diffRegion = screen.getByLabelText('修订建议')
    await user.click(within(diffRegion).getByRole('button', { name: '采纳' }))
    await user.click(within(tree).getAllByRole('button', { name: '采纳' })[1])
    await user.click(within(tree).getAllByRole('button', { name: '采纳' })[2])
    await user.click(within(tree).getAllByRole('button', { name: '拒绝' })[0])
    const textarea = screen.getByRole('textbox')
    await user.clear(textarea)
    await user.type(textarea, '  按决策树调整再搜  ')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith('按决策树调整再搜\n【逐项采纳】diff-1\n【采纳步骤】exp-2；exp-3 【拒绝步骤】exp-1')
  })

  it('无树的旧复盘不渲染树区块，diff 采纳与提交后缀不回归', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    stubReviewFetch(legacyPayload)
    render(<RevisePlanDialog workflowId="wf-1" onCancel={() => undefined} onSubmit={onSubmit} />)
    const diffRegion = await screen.findByLabelText('修订建议')
    expect(screen.queryByLabelText('人不够时的扩圈建议')).not.toBeInTheDocument()
    await user.click(within(diffRegion).getByRole('button', { name: '采纳' }))
    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveValue('增列目标公司池：T2：甲公司、乙公司')
    await user.click(screen.getByRole('button', { name: '确认修改' }))
    expect(onSubmit).toHaveBeenCalledWith('增列目标公司池：T2：甲公司、乙公司\n【逐项采纳】diff-1')
  })
})

describe('工作流面板：不再提供本地树决策入口', () => {
  it('仅展示 Copilot 交接入口', () => {
    render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={() => undefined} reload={vi.fn()} openCandidate={() => undefined} archived={() => undefined} />)
    expect(screen.getByRole('button', { name: '在 Copilot 中讨论策略' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改计划' })).not.toBeInTheDocument()
  })
})
