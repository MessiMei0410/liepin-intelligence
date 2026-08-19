import { z } from 'zod'
import type { AgentContext, AgentReference } from './transport'

const contextSchema = z.object({
  type: z.string().optional(),
  id: z.union([z.string(), z.number()]).nullable().optional(),
}).catchall(z.unknown())

const referenceSchema = z.object({
  type: z.string(),
  id: z.union([z.string(), z.number()]),
  label: z.string(),
  subtitle: z.string().nullish().transform(value => value || undefined),
  href: z.string().nullish().transform(value => value || undefined),
})

const structuredRecord = z.record(z.string(), z.unknown())

const sessionSummarySchema = z.object({
  session_id: z.string().min(1),
  title: z.string(),
  preview: z.string(),
  message_count: z.number().int().nonnegative(),
  updated_at: z.string().optional(),
  context_type: z.string().optional(),
  context_id: z.union([z.string(), z.number(), z.null()]).optional(),
  business_focus: structuredRecord.nullable().optional(),
  archived: z.boolean().optional(),
})

const messageSchema = z.object({
  role: z.enum(['user', 'assistant']),
  content: z.string(),
  // DSH 思考过程（流式 thinking 事件聚合；恢复会话时 Core 暂未回填则缺省）。
  thinking: z.string().optional(),
  context: contextSchema.optional(),
  references: z.array(referenceSchema).optional(),
  suggested_actions: z.array(structuredRecord).optional(),
  understanding_card: structuredRecord.nullable().optional(),
  execution_receipt: structuredRecord.nullable().optional(),
  business_focus: structuredRecord.nullable().optional(),
  workflow_id: z.string().nullable().optional(),
  workflow_progress: structuredRecord.nullable().optional(),
  pending_intent: structuredRecord.nullable().optional(),
  action_card: structuredRecord.nullable().optional(),
  // 复数卡片（DSH 委托载荷 action_cards；恢复会话时 Core 回填）：candidate_list 可能
  // 只在数组里而不在单卡 action_card 上，常驻「查看名单」入口与名单卡抑制都要看它。
  action_cards: z.array(structuredRecord).optional(),
  // DSH 写确认卡（含 state 终态：pending/confirmed/cancelled；过期由前端按 expires_at 判定）。
  confirm_request: structuredRecord.nullable().optional(),
  model_participation: structuredRecord.nullable().optional(),
  strategy_patch: structuredRecord.nullable().optional(),
  strategy_patch_applied: z.boolean().optional(),
  strategy_patch_ignored: z.boolean().optional(),
  strategy_patch_revision: z.number().nullable().optional(),
  strategy_patch_artifact_id: z.string().nullable().optional(),
  strategy_patch_applied_count: z.number().nullable().optional(),
  invalidated: z.boolean().optional(),
  invalidated_reason: z.string().optional(),
  revoked_actions: z.array(structuredRecord).optional(),
  created_at: z.string().optional(),
})

const sessionListSchema = z.object({
  ok: z.boolean(),
  sessions: z.array(sessionSummarySchema),
})

const sessionSchema = z.object({
  ok: z.boolean(),
  session_id: z.string().min(1),
  messages: z.array(messageSchema),
  business_focus: structuredRecord.nullable().optional(),
  // 会话历史分页（Core 支持 offset 后返回；旧 Core 缺字段时按 0/false 兜底）。
  total: z.number().int().nonnegative().optional().default(0),
  has_more: z.boolean().optional().default(false),
})

const sessionUpdateSchema = z.object({
  ok: z.boolean(), session_id: z.string().min(1), title: z.string(), archived: z.boolean(),
  business_focus: structuredRecord.nullable().optional(),
})

const sessionSearchMatchSchema = z.object({
  role: z.enum(['user', 'assistant']),
  created_at: z.string().nullable().optional(),
  content: z.string(),
  snippet: z.string(),
  newer_count: z.number().int().nonnegative(),
})

const sessionSearchSchema = z.object({
  ok: z.boolean(),
  session_id: z.string(),
  query: z.string(),
  matches: z.array(sessionSearchMatchSchema),
  total: z.number().int().nonnegative().optional().default(0),
})

export type AgentSessionSummary = z.infer<typeof sessionSummarySchema>
export type AgentMessage = Omit<z.infer<typeof messageSchema>, 'context' | 'references'> & {
  context?: AgentContext
  references?: AgentReference[]
}
export type AgentSession = Omit<z.infer<typeof sessionSchema>, 'messages'> & { messages: AgentMessage[] }
export type AgentSessionUpdate = z.infer<typeof sessionUpdateSchema>
export type AgentSessionSearchMatch = z.infer<typeof sessionSearchMatchSchema>

export const parseAgentSessionList = (value: unknown): { ok: boolean; sessions: AgentSessionSummary[] } => sessionListSchema.parse(value)
export const parseAgentSession = (value: unknown): AgentSession => sessionSchema.parse(value) as AgentSession
export const parseAgentSessionSearch = (value: unknown): z.infer<typeof sessionSearchSchema> => sessionSearchSchema.parse(value)
export const parseAgentSessionUpdate = (value: unknown): AgentSessionUpdate => sessionUpdateSchema.parse(value)
