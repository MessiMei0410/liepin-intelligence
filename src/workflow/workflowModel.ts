import { z } from 'zod'

// R5 工作流详情 payload 边界模型。字段口径与原 api.ts 手写 Workflow 类型、
// statusMapping.ts 及 __tests__/helpers.ts fixture 对齐：结构字段（goal/workflow/steps/
// approvals/artifacts）严格，标量叶子随 Core 演进保持宽松，未知键一律 loose 透传。
// business_outcome 刻意不做枚举校验——未知新值由 statusMapping 兜底渲染，schema 不应因此判负。
// 本地快速迭代期 parse 失败不得白屏：parseWorkflow 校验失败时降级为宽松透传并 console.warn。

const recordValue = z.record(z.string(), z.unknown())

const workflowStepSchema = z.looseObject({
  id: z.number(),
  sequence: z.number(),
  business_label: z.string(),
  reason: z.string().optional(),
  risk_level: z.string(),
  status: z.string(),
  capability_id: z.string().optional(),
  output: recordValue.optional(),
  output_json: z.string().optional(),
  error: z.string().optional(),
  verification: z.looseObject({
    ok: z.boolean().optional(),
    status: z.string().optional(),
    summary: z.string().optional(),
    checks: z.array(recordValue).optional(),
  }).optional(),
  recovery: z.looseObject({
    action: z.string().optional(),
    reason: z.string().optional(),
    attempt: z.number().optional(),
    max_attempts: z.number().optional(),
  }).optional(),
  started_at: z.string().optional(),
  finished_at: z.string().optional(),
  updated_at: z.string().optional(),
})

export const workflowSchema = z.looseObject({
  ok: z.boolean(),
  business_outcome: z.string().nullish(),
  goal: z.looseObject({
    title: z.string(),
    objective: z.string(),
    status: z.string(),
    progress: z.number(),
    started_at: z.string().optional(),
    finished_at: z.string().optional(),
    error: z.string().optional(),
    business_outcome: z.string().nullish(),
    context: z.looseObject({
      type: z.string().optional(),
      id: z.number().optional(),
      page: z.string().optional(),
    }).optional(),
  }),
  workflow: z.looseObject({
    workflow_id: z.string(),
    status: z.string(),
    current_stage: z.string().optional(),
    updated_at: z.string().optional(),
    started_at: z.string().optional(),
    finished_at: z.string().optional(),
    active_step_id: z.number().optional(),
    archived_at: z.string().optional(),
    business_outcome: z.string().nullish(),
  }),
  progress: z.looseObject({
    completed: z.number(),
    total: z.number(),
    ratio: z.number(),
  }).optional(),
  steps: z.array(workflowStepSchema),
  approvals: z.array(z.looseObject({
    approval_id: z.string(),
    title: z.string(),
    risk_level: z.string(),
    status: z.string(),
    created_at: z.string(),
    expires_at: z.string().optional(),
    preflight: z.looseObject({
      before: z.string().optional(),
      after: z.string().optional(),
      channel: z.string().optional(),
      object_label: z.string().optional(),
      action: z.string().optional(),
    }).optional(),
  })),
  artifacts: z.array(z.looseObject({
    artifact_id: z.string(),
    title: z.string(),
    artifact_type: z.string(),
    validation_status: z.string(),
  })),
  events: z.array(z.looseObject({
    id: z.number(),
    event_type: z.string(),
    status: z.string(),
    summary: z.string(),
    created_at: z.string(),
  })).optional(),
})

export type Workflow = z.infer<typeof workflowSchema>

// 工作流面板人选行：由 steps output 动态提取、合并（recordValue 展开 + 面板补充的评分字段），
// 字段随 Core 寻访/评估产物演进，保持开放，仅收敛面板必用的几枚。
export type WorkflowCandidateRow = Record<string, unknown> & {
  jobCandidateId?: number
  searchScore?: number
  searchLevel?: string
  assessmentScore?: number
  recommendation?: string
}

// 边界 parse：成功返回 schema 收窄后的 payload（未知键透传）；
// 失败降级为宽松透传并告警，渲染行为与校验前一致，绝不因 schema 漂移白屏。
export const parseWorkflow = (payload: unknown): Workflow => {
  const result = workflowSchema.safeParse(payload)
  if (result.success) return result.data
  console.warn('[workflowModel] 工作流 payload 与 schema 漂移，降级为宽松透传。', result.error.issues)
  return payload as Workflow
}
