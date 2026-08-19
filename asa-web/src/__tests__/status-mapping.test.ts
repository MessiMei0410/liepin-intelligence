import { describe, expect, it } from 'vitest'
import { mapWorkflowStatus, intentionLabel, humanizeEventStatus } from '../workflow/statusMapping'

const steps = [
  { status: 'completed', business_label: '生成寻访策略' },
  { status: 'failed', business_label: '执行多渠道寻访' },
  { status: 'failed', business_label: '评估候选人' },
]

describe('mapWorkflowStatus 业务终态映射', () => {
  it('failed + business_outcome null → 技术失败（红），附第一个失败步骤名', () => {
    const mapped = mapWorkflowStatus({ status: 'failed', business_outcome: null, steps })
    expect(mapped).toMatchObject({ tone: 'red', kind: 'technical_failed', showNextActions: false })
    expect(mapped.label).toBe('技术失败：执行多渠道寻访')
  })

  it('failed 且无失败步骤/无步骤名 → 省略步骤名', () => {
    expect(mapWorkflowStatus({ status: 'failed', business_outcome: null }).label).toBe('技术失败')
    expect(mapWorkflowStatus({ status: 'failed', business_outcome: null, steps: [{ status: 'failed', business_label: '  ' }] }).label).toBe('技术失败')
  })

  it('business_outcome=failed_technical 即使 status 非 failed 也按技术失败处理', () => {
    const mapped = mapWorkflowStatus({ status: 'blocked', business_outcome: 'failed_technical', steps })
    expect(mapped).toMatchObject({ tone: 'red', kind: 'technical_failed', showNextActions: false })
    expect(mapped.label).toBe('技术失败：执行多渠道寻访')
  })

  it('completed_target_met → 本轮完成，达成目标（绿），无下一步按钮', () => {
    expect(mapWorkflowStatus({ status: 'completed', business_outcome: 'completed_target_met' })).toEqual({
      label: '本轮完成，达成目标', tone: 'green', kind: 'target_met', showNextActions: false,
    })
  })

  it('completed_needs_review → 有待复核人选（amber），展示下一步按钮', () => {
    expect(mapWorkflowStatus({ status: 'blocked', business_outcome: 'completed_needs_review' })).toEqual({
      label: '本轮完成，合格人数不足，有待复核人选', tone: 'amber', kind: 'needs_review', showNextActions: true,
    })
  })

  it('completed_pool_insufficient → 合格人数不足（amber），展示下一步按钮', () => {
    expect(mapWorkflowStatus({ status: 'blocked', business_outcome: 'completed_pool_insufficient' })).toEqual({
      label: '本轮完成，合格人数不足', tone: 'amber', kind: 'pool_insufficient', showNextActions: true,
    })
  })

  it('blocked + business_outcome null → 流程阻塞，待处理（muted），无下一步按钮，与业务未达标区分', () => {
    const mapped = mapWorkflowStatus({ status: 'blocked', business_outcome: null })
    expect(mapped).toEqual({ label: '流程阻塞，待处理', tone: 'muted', kind: 'flow_blocked', showNextActions: false })
    expect(mapped.label).not.toContain('合格人数不足')
  })

  it('business_outcome 出现未知新值 → 回落 status 原有逻辑，绝不渲染英文枚举原形', () => {
    const mapped = mapWorkflowStatus({ status: 'blocked', business_outcome: 'completed_partial_refill' })
    expect(mapped.label).toBe('已阻塞')
    expect(mapped.kind).toBe('default')
    expect(mapped.showNextActions).toBe(false)
    expect(mapped.label).not.toContain('completed_partial_refill')
  })

  it('活跃与常规状态沿用现有文案', () => {
    expect(mapWorkflowStatus({ status: 'running', business_outcome: null })).toMatchObject({ label: '执行中', tone: 'muted', kind: 'default', showNextActions: false })
    expect(mapWorkflowStatus({ status: 'waiting_approval', business_outcome: null })).toMatchObject({ label: '等待审批', tone: 'amber', kind: 'default' })
    expect(mapWorkflowStatus({ status: 'completed', business_outcome: null })).toMatchObject({ label: '已完成', tone: 'green', kind: 'default' })
    expect(mapWorkflowStatus({ status: 'cancelled', business_outcome: null })).toMatchObject({ label: '已取消', tone: 'muted', kind: 'default' })
  })

  it('superseded → 已被修订版替代，不渲染英文原形', () => {
    const mapped = mapWorkflowStatus({ status: 'superseded', business_outcome: null })
    expect(mapped.label).toBe('已被修订版替代')
    expect(mapped.label).not.toContain('superseded')
  })

  it('未知 status → 中文兜底，不渲染英文原形', () => {
    expect(mapWorkflowStatus({ status: 'mystery_state', business_outcome: null }).label).toBe('状态待同步')
  })
})

describe('intentionLabel 寻访名单意向列枚举收口（P8）', () => {
  it('raw_status 枚举一律映射为中文', () => {
    expect(intentionLabel('screen_rejected')).toBe('初筛未通过')
    expect(intentionLabel('xsaas_review_stop')).toBe('复核未通过')
    expect(intentionLabel('search_shortlisted')).toBe('搜索入库 · 待复核')
    expect(intentionLabel('xsaas_search_shortlisted')).toBe('X-SaaS 入库 · 待复核')
    expect(intentionLabel('rejected')).toBe('已淘汰')
    expect(intentionLabel('stopped')).toBe('已停止')
    expect(intentionLabel('closed')).toBe('已关闭')
    expect(intentionLabel('candidate_intake')).toBe('已入库')
  })

  it('自由中文文本原样保留，空值为占位符', () => {
    expect(intentionLabel('对方近期沟通过【上海】地区机会')).toBe('对方近期沟通过【上海】地区机会')
    expect(intentionLabel('')).toBe('-')
    expect(intentionLabel(undefined)).toBe('-')
  })

  it('漏网的纯英文枚举不直出，回落中文兜底', () => {
    expect(intentionLabel('some_new_status')).toBe('状态待同步')
  })
})

describe('humanizeEventStatus 时间线事件状态收口（P8）', () => {
  it('增补枚举映射为中文', () => {
    expect(humanizeEventStatus('corrected')).toBe('已纠正')
    expect(humanizeEventStatus('job_chat_verified')).toBe('猎聘触达已核验')
  })

  it('既有映射继续生效', () => {
    expect(humanizeEventStatus('completed')).toBe('已完成')
    expect(humanizeEventStatus('verified')).toBe('已核验')
    expect(humanizeEventStatus('pending_review')).toBe('待复核')
  })

  it('未知英文枚举不渲染原形，空值返回空串', () => {
    expect(humanizeEventStatus('brand_new_status')).toBe('状态待同步')
    expect(humanizeEventStatus('')).toBe('')
    expect(humanizeEventStatus(undefined)).toBe('')
  })
})
