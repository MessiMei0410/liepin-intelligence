import { describe, expect, it } from 'vitest'
import { mapWorkflowStatus } from '../workflow/statusMapping'

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

  it('未知 status → 原样透出（与现有行为等价）', () => {
    expect(mapWorkflowStatus({ status: 'mystery_state', business_outcome: null }).label).toBe('mystery_state')
  })
})
