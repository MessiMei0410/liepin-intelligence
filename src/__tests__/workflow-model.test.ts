import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseWorkflow, workflowSchema } from '../workflow/workflowModel'
import { plannedWorkflow } from './helpers'

describe('workflowModel 边界校验', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('helpers fixture 本身满足 schema（口径锚定）', () => {
    expect(workflowSchema.safeParse(plannedWorkflow).success).toBe(true)
  })

  it('合法 payload 正常 parse，未知扩展字段 loose 透传', () => {
    const parsed = parseWorkflow({ ...plannedWorkflow, future_field: { a: 1 } })
    expect(parsed.workflow.workflow_id).toBe('wf-1')
    expect(parsed.steps).toHaveLength(2)
    expect((parsed as Record<string, unknown>).future_field).toEqual({ a: 1 })
  })

  it('缺关键字段时 console.warn 并降级为宽松透传（原样返回，不抛错）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const broken = { ok: true, goal: plannedWorkflow.goal }
    const result = parseWorkflow(broken)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('schema')
    expect(result).toBe(broken)
  })

  it('business_outcome 未知新值不做枚举校验，原样透传且不告警', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const parsed = parseWorkflow({ ...plannedWorkflow, business_outcome: 'completed_partial_refill' })
    expect(parsed.business_outcome).toBe('completed_partial_refill')
    expect(warn).not.toHaveBeenCalled()
  })
})
