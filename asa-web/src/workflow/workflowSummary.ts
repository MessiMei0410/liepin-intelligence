import { z } from 'zod'
import { workflowStepSchema } from './workflowModel'
import type { Workflow } from './workflowModel'

// R7 轮询减负：工作流摘要 / 步骤详情 / 人选分页三个增量路由的边界模型。
// 与 workflowModel 同一纪律：结构字段严格、标量叶子宽松、looseObject 透传未知键；
// parse 失败降级为宽松透传并 console.warn，绝不因 schema 漂移白屏。

const recordValue = z.record(z.string(), z.unknown())

// GET /api/v1/workflows/{id}/summary：轮询用小 payload（不含 steps 全文 / audit stdout / events 全量）。
export const workflowSummarySchema = z.looseObject({
  ok: z.boolean(),
  workflow_id: z.string(),
  title: z.string().optional(),
  status: z.string(),
  business_outcome: z.string().nullish(),
  progress: z.looseObject({
    completed: z.number(),
    total: z.number(),
    ratio: z.number(),
  }).optional(),
  current_stage: z.string().optional(),
  next_step: recordValue.nullish(),
  pending_approvals: z.array(recordValue).optional(),
  recent_artifacts: z.array(recordValue).optional(),
  recent_events: z.array(z.looseObject({
    id: z.number(),
    event_type: z.string().optional(),
    status: z.string().optional(),
    summary: z.string().optional(),
    created_at: z.string().optional(),
  })).optional(),
})
export type WorkflowSummary = z.infer<typeof workflowSummarySchema>

// GET /api/v1/workflows/{id}/steps/{step_id}：单步完整 output（audit stdout、assessed_items），
// 响应包一层 { step }，step 与详情 steps[] 项同构（另有 step_key/depends_on 等透传字段）。
export const workflowStepDetailSchema = z.looseObject({
  ok: z.boolean(),
  workflow_id: z.string().optional(),
  step: workflowStepSchema,
})
export type WorkflowStepDetail = z.infer<typeof workflowStepDetailSchema>

// GET /api/v1/workflows/{id}/candidates?limit&offset：工作流人选摘要分页，total 在响应里。
// 注意没有 city/experience/education——行内展示以 fit_level / stage 为准，勿回退读大对象 assessed_items。
export const workflowCandidateItemSchema = z.looseObject({
  id: z.number(),
  person_id: z.number().optional(),
  name: z.string().optional(),
  company: z.string().optional(),
  title: z.string().optional(),
  fit_score: z.number().nullish(),
  fit_level: z.string().optional(),
  recommendation: z.string().optional(),
  stage: z.string().optional(),
  flow_bucket: z.string().optional(),
  status: z.string().optional(),
  assessed: z.boolean().optional(),
  attribution: z.looseObject({
    channel: z.string().optional(),
    source_query: z.string().optional(),
    source_round: z.string().optional(),
    from_workflow: z.boolean().optional(),
  }).nullish(),
  updated_at: z.string().optional(),
})
export type WorkflowCandidateItem = z.infer<typeof workflowCandidateItemSchema>

export const workflowCandidatesPageSchema = z.looseObject({
  ok: z.boolean(),
  items: z.array(workflowCandidateItemSchema),
  total: z.number(),
  limit: z.number().optional(),
  offset: z.number().optional(),
})
export type WorkflowCandidatesPage = z.infer<typeof workflowCandidatesPageSchema>

// 与 parseWorkflow 同款降级：漂移时宽松透传并告警，渲染行为不受影响。
const parseWith = <S extends z.ZodType>(schema: S, label: string, payload: unknown): z.infer<S> => {
  const result = schema.safeParse(payload)
  if (result.success) return result.data as z.infer<S>
  console.warn(`[workflowSummary] ${label} payload 与 schema 漂移，降级为宽松透传。`, result.error.issues)
  return payload as z.infer<S>
}

export const parseWorkflowSummary = (payload: unknown): WorkflowSummary => parseWith(workflowSummarySchema, '工作流摘要', payload)
export const parseWorkflowStepDetail = (payload: unknown): WorkflowStepDetail => parseWith(workflowStepDetailSchema, '步骤详情', payload)
// 漂移降级路径也要保证列表形状：items/total 缺失时归一化为空页，渲染层不做防御判断。
export const parseWorkflowCandidatesPage = (payload: unknown): WorkflowCandidatesPage => {
  const page = parseWith(workflowCandidatesPageSchema, '工作流人选', payload)
  return { ...page, items: Array.isArray(page.items) ? page.items : [], total: Number(page.total) || 0 }
}

// 变化检测口径（R7 任务口径）：status / business_outcome / progress / pending_approvals，
// 另附最近事件 id 兜底——任何步骤推进都会落事件，revise 等不改前四字段的动作也不会漏检。
// 详情与摘要两侧的事件列表均按 id 倒序（首条即最新），投影口径一致。
export const summarySignature = (summary: WorkflowSummary): string => [
  summary.status,
  summary.business_outcome ?? '',
  summary.progress ? `${summary.progress.completed}/${summary.progress.total}` : '',
  (summary.pending_approvals || []).map(item => String(item.approval_id || '')).join(','),
  String(summary.recent_events?.[0]?.id || ''),
].join('|')

export const workflowDetailSignature = (workflow: Workflow): string => [
  workflow.workflow.status,
  workflow.business_outcome ?? workflow.workflow.business_outcome ?? workflow.goal.business_outcome ?? '',
  workflow.progress ? `${workflow.progress.completed}/${workflow.progress.total}` : '',
  workflow.approvals.filter(item => item.status === 'pending').map(item => item.approval_id).join(','),
  String(workflow.events?.[0]?.id || ''),
].join('|')
