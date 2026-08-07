import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { api } from '../api'
import { WorkflowStrategy } from '../workflows/WorkflowStrategy'

// 寻访策略按项编辑（一期）：组级目标画像/预计质量渲染 + 按项编辑入口。
// 编辑走 POST /strategy/edits（api.applyStrategyEdits，幂等封装）；失败保留原数据并显示错误。

const baseStrategy = { generation: { mode: 'llm', model: 'ASA Model' } }
const baseChannels = {
  liepin: [{ query: '服务器电源', purpose: '核心画像' }],
  xsaas: [{ query: '日立', purpose: 'T1 公司' }],
}
const baseV2 = {
  step2_target_pool: [
    { path: 'same_layer', tier: 'T1', rationale: 'T1 竞对', companies: [
      { name: '日立', source: 'client_doc', confidence: 'high' },
      { name: '台达', source: 'client_doc', confidence: 'medium' },
    ] },
  ],
  step3_level_mapping: { accepted_levels: ['P5', 'P6'], calibration_rule: '按职责定档' },
  step4_keyword_groups: [
    { group: 'core_power', targets: 'T1 友商电源工程师', terms: ['服务器电源', '电源模块'] },
    { group: 'scene', targets: '', terms: ['储能电源'] },
  ],
  step5_expectation: { expected_recall_per_tier: { T1: 5, T2: 8 }, fallback_plan: '放宽相邻池' },
  consultant_judgement: {
    version: 'senior_consultant_v1',
    basis: ['岗位事实', '客户画像', '历史实验/业务反馈'],
    role_diagnosis: {
      role_family: '研发/工程',
      business_mandate: '解决服务器板级供电设计与验证的人才缺口',
      candidate_archetype: '能独立负责服务器电源模块设计、仿真和验证闭环的人选',
    },
    market_view: { reason: '当前可执行公司池 12 家，需要分轮扩池' },
    search_sequence: [
      { round: 'R1', name: '核心同层', purpose: '先验证服务器电源直接项目证据' },
      { round: 'R2', name: '岗位原型变体', purpose: '补齐 title 不同但职责相同的人选' },
    ],
    expansion_ladder: [
      { step: 1, direction: '同层目标公司', trigger: '首轮建立直接证据基线', tradeoff: '池子相对小' },
      { step: 2, direction: '相邻产品/场景迁移', trigger: '核心池不足且客户接受迁移', tradeoff: '需要项目补证' },
    ],
    evidence_standard: { must_verify: ['真实职责边界', '具体项目和结果'] },
    client_calibration: { must_confirm: ['确认一票否决项', '确认薪资和汇报线'] },
  },
}

function renderPanel(overrides: Partial<Parameters<typeof WorkflowStrategy>[0]> = {}) {
  return render(
    <WorkflowStrategy
      strategy={baseStrategy}
      channels={baseChannels}
      gates={{}}
      open
      toggle={() => undefined}
      strategyV2={baseV2}
      workflowId="workflow_edit1"
      editable
      onEdited={() => undefined}
      {...overrides}
    />,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('组级目标画像与预计质量渲染', () => {
  it('每组关键词显示 targets；空 targets 显示「未提供目标画像」', () => {
    renderPanel()
    const detail = document.querySelector('.strategy-v2-detail') as HTMLElement
    expect(within(detail).getByText('目标画像：T1 友商电源工程师')).toBeInTheDocument()
    expect(within(detail).getByText('目标画像：未提供目标画像')).toBeInTheDocument()
  })

  it('显示预期召回分层与兜底计划；缺数据时如实显示「未提供预期召回」', () => {
    renderPanel()
    expect(screen.getByText('T1：5')).toBeInTheDocument()
    expect(screen.getByText('T2：8')).toBeInTheDocument()
    expect(screen.getByText(/放宽相邻池/)).toBeInTheDocument()

    cleanup()
    renderPanel({ strategyV2: { ...baseV2, step5_expectation: {} } })
    expect(screen.getByText('未提供预期召回')).toBeInTheDocument()
  })

  it('显示资深顾问判断：岗位本质、搜索顺序、扩池边界和校准项', () => {
    renderPanel()
    const brief = screen.getByRole('region', { name: '资深顾问判断' })
    expect(within(brief).getByText('顾问判断')).toBeInTheDocument()
    expect(within(brief).getByText('解决服务器板级供电设计与验证的人才缺口')).toBeInTheDocument()
    expect(within(brief).getByText('R1 · 核心同层')).toBeInTheDocument()
    expect(within(brief).getByText('2 · 相邻产品/场景迁移')).toBeInTheDocument()
    expect(within(brief).getByText(/确认薪资和汇报线/)).toBeInTheDocument()
  })

  it('折叠态不渲染组级明细；无 strategy_v2 时不渲染编辑区', () => {
    renderPanel({ open: false })
    expect(document.querySelector('.strategy-v2-detail')).toBeNull()
    cleanup()
    renderPanel({ strategyV2: undefined })
    expect(document.querySelector('.strategy-v2-detail')).toBeNull()
  })

  it('editable=false 时不提供编辑入口', () => {
    renderPanel({ editable: false })
    expect(screen.queryByLabelText('编辑关键词组 core_power')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('删除 T1 池公司 台达')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('修改职级映射')).not.toBeInTheDocument()
  })
})

describe('按项编辑交互', () => {
  it('编辑关键词组：提交 terms+targets，成功显示回执并触发 onEdited', async () => {
    const apply = vi.spyOn(api, 'applyStrategyEdits').mockResolvedValue({
      ok: true, revision: 2, applied: [{ op: 'update_keyword_group', summary: '更新关键词组「core_power」' }],
    })
    const onEdited = vi.fn()
    renderPanel({ onEdited })

    fireEvent.click(screen.getByLabelText('编辑关键词组 core_power'))
    const termsInput = screen.getByLabelText('关键词组 core_power 的词条')
    const targetsInput = screen.getByLabelText('关键词组 core_power 的目标画像')
    fireEvent.change(termsInput, { target: { value: '通信电源\n基站电源' } })
    fireEvent.change(targetsInput, { target: { value: '基站电源方向' } })
    fireEvent.click(screen.getByLabelText('保存关键词组 core_power'))

    expect(apply).toHaveBeenCalledWith('workflow_edit1', [
      { op: 'update_keyword_group', group: 'core_power', terms: ['通信电源', '基站电源'], targets: '基站电源方向' },
    ])
    expect(await screen.findByText(/已保存为策略 revision 2/)).toBeInTheDocument()
    expect(onEdited).toHaveBeenCalled()
  })

  it('删除公司为两步确认，中途可取消', async () => {
    const apply = vi.spyOn(api, 'applyStrategyEdits').mockResolvedValue({ ok: true, revision: 1, applied: [] })
    renderPanel()

    fireEvent.click(screen.getByLabelText('删除 T1 池公司 台达'))
    expect(apply).not.toHaveBeenCalled()
    expect(screen.getByLabelText('确认删除 T1 池公司 台达')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('取消删除'))
    expect(screen.queryByLabelText('确认删除 T1 池公司 台达')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('删除 T1 池公司 台达'))
    fireEvent.click(screen.getByLabelText('确认删除 T1 池公司 台达'))
    expect(apply).toHaveBeenCalledWith('workflow_edit1', [{ op: 'delete_company', tier: 'T1', name: '台达' }])
    expect(await screen.findByText(/已保存为策略 revision 1/)).toBeInTheDocument()
  })

  it('修改职级：顿号分隔输入提交 update_accepted_levels', async () => {
    const apply = vi.spyOn(api, 'applyStrategyEdits').mockResolvedValue({ ok: true, revision: 3, applied: [] })
    renderPanel()

    fireEvent.click(screen.getByLabelText('修改职级映射'))
    fireEvent.change(screen.getByLabelText('可接受职级'), { target: { value: 'P6、P7' } })
    fireEvent.click(screen.getByLabelText('保存职级映射'))
    expect(apply).toHaveBeenCalledWith('workflow_edit1', [{ op: 'update_accepted_levels', accepted_levels: ['P6', 'P7'] }])
    expect(await screen.findByText(/已保存为策略 revision 3/)).toBeInTheDocument()
  })

  it('提交失败保留原数据并显示后端可读错误', async () => {
    vi.spyOn(api, 'applyStrategyEdits').mockRejectedValue(new Error('公司词不能两两成对'))
    const onEdited = vi.fn()
    renderPanel({ onEdited })

    fireEvent.click(screen.getByLabelText('删除 T1 池公司 台达'))
    fireEvent.click(screen.getByLabelText('确认删除 T1 池公司 台达'))
    expect(await screen.findByRole('alert')).toHaveTextContent('公司词不能两两成对')
    // 原数据保留：公司芯片与关键词组仍按编辑前渲染
    expect(screen.getByText('台达')).toBeInTheDocument()
    expect(screen.getByText('目标画像：T1 友商电源工程师')).toBeInTheDocument()
    expect(onEdited).not.toHaveBeenCalled()
  })
})
