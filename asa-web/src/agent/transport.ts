import { z } from 'zod'

export type AgentContext = Record<string, unknown> & { type?: string; id?: string | number | null }

export type AgentReference = {
  type: string
  id: string | number
  label: string
  subtitle?: string
  href?: string
}

export type AgentTurnResult = {
  ok?: boolean
  session_id: string
  answer: string
  error?: string
  context?: AgentContext
  references?: AgentReference[]
  suggested_actions?: Array<Record<string, unknown>>
  understanding_card?: Record<string, unknown> | null
  execution_receipt?: Record<string, unknown> | null
  business_focus?: Record<string, unknown> | null
  workflow_id?: string | null
  workflow_progress?: Record<string, unknown> | null
  workflow?: Record<string, unknown> | null
  progress?: Record<string, unknown> | null
  approvals?: Array<Record<string, unknown>>
  plan_summary?: Array<Record<string, unknown>>
  goal?: Record<string, unknown> | null
  pending_intent?: Record<string, unknown> | null
  action_card?: Record<string, unknown> | null
  model_participation?: Record<string, unknown> | null
  strategy_patch?: Record<string, unknown> | null
  strategy_patch_applied?: boolean
  strategy_patch_ignored?: boolean
  strategy_patch_revision?: number | null
  strategy_patch_artifact_id?: string | null
  strategy_patch_applied_count?: number | null
  invalidated?: boolean
  invalidated_reason?: string
  revoked_actions?: Array<Record<string, unknown>>
}

export type AgentSseEvent =
  | { type: 'context'; data: { session_id: string; context?: AgentContext; references?: AgentReference[]; suggested_actions?: Array<Record<string, unknown>> } }
  | { type: 'progress'; data: { message: string } }
  | { type: 'text'; data: { content: string } }
  | { type: 'done'; data: AgentTurnResult }
  | { type: 'error'; data: { error: string } }

const structuredRecord = z.record(z.string(), z.unknown())
const contextSchema = z.object({
  type: z.string().optional(), id: z.union([z.string(), z.number()]).nullable().optional(),
}).catchall(z.unknown())
const referenceSchema = z.object({
  type: z.string(), id: z.union([z.string(), z.number()]), label: z.string(),
  subtitle: z.string().nullish().transform(value => value || undefined),
  href: z.string().nullish().transform(value => value || undefined),
})
const contextEventSchema = z.object({
  session_id: z.string().min(1), context: contextSchema.optional(), references: z.array(referenceSchema).optional(),
  suggested_actions: z.array(structuredRecord).optional(),
})
const textEventSchema = z.object({ content: z.string() })
const progressEventSchema = z.object({ message: z.string() })
const doneEventSchema = z.object({
  ok: z.boolean().optional(), session_id: z.string().min(1), answer: z.string(), error: z.string().optional(), context: contextSchema.optional(),
  references: z.array(referenceSchema).optional(), suggested_actions: z.array(structuredRecord).optional(),
  business_focus: structuredRecord.nullable().optional(), workflow_id: z.string().nullable().optional(),
  workflow: structuredRecord.nullable().optional(), progress: structuredRecord.nullable().optional(),
  approvals: z.array(structuredRecord).optional(), plan_summary: z.array(structuredRecord).optional(),
  goal: structuredRecord.nullable().optional(),
  workflow_progress: structuredRecord.nullable().optional(), pending_intent: structuredRecord.nullable().optional(),
  action_card: structuredRecord.nullable().optional(),
  understanding_card: structuredRecord.nullable().optional(),
  execution_receipt: structuredRecord.nullable().optional(),
  model_participation: structuredRecord.nullable().optional(),
  strategy_patch: structuredRecord.nullable().optional(),
  strategy_patch_applied: z.boolean().optional(),
  strategy_patch_ignored: z.boolean().optional(),
  strategy_patch_revision: z.number().nullable().optional(),
  strategy_patch_artifact_id: z.string().nullable().optional(),
  strategy_patch_applied_count: z.number().nullable().optional(),
  invalidated: z.boolean().optional(), invalidated_reason: z.string().optional(),
  revoked_actions: z.array(structuredRecord).optional(),
})
const errorEventSchema = z.object({ error: z.string() })

const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object' ? value as Record<string, unknown> : {}

const synthesizeWorkflowProgress = (data: AgentTurnResult): Record<string, unknown> | null => {
  if (data.workflow_progress) return data.workflow_progress
  const workflowId = String(data.workflow_id || '').trim()
  if (!workflowId) return null
  const workflow = asRecord(data.workflow)
  const progress = asRecord(data.progress)
  const planSummary = Array.isArray(data.plan_summary) ? data.plan_summary : []
  const approvals = Array.isArray(data.approvals) ? data.approvals.filter(item => asRecord(item).status === 'pending') : []
  const completed = Number(progress.completed ?? 0)
  const total = Number(progress.total ?? planSummary.length ?? 0)
  return {
    workflow_id: workflowId,
    status: workflow.status || 'planned',
    completed: Number.isFinite(completed) ? completed : 0,
    total: Number.isFinite(total) ? total : 0,
    label: workflow.current_stage || progress.label || '准备执行',
    pending_approvals: approvals,
  }
}

const parseEvent = (block: string): AgentSseEvent | undefined => {
  const lines = block.split(/\r?\n/)
  const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim()
  const rawData = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
  if (!event || !rawData) return undefined
  let value: unknown
  try { value = JSON.parse(rawData) } catch { return { type: 'error', data: { error: 'Agent 返回了无法解析的数据' } } }
  const parsed = event === 'context' ? contextEventSchema.safeParse(value)
    : event === 'text' ? textEventSchema.safeParse(value)
      : event === 'progress' ? progressEventSchema.safeParse(value)
        : event === 'done' ? doneEventSchema.safeParse(value)
          : event === 'error' ? errorEventSchema.safeParse(value)
            : undefined
  if (!parsed) return undefined
  if (!parsed.success) return { type: 'error', data: { error: 'Agent 返回数据与约定格式不一致' } }
  if (event === 'context') return { type: 'context', data: parsed.data as Extract<AgentSseEvent, { type: 'context' }>['data'] }
  if (event === 'text') return { type: 'text', data: parsed.data as Extract<AgentSseEvent, { type: 'text' }>['data'] }
  if (event === 'progress') return { type: 'progress', data: parsed.data as { message: string } }
  if (event === 'done') {
    const data = parsed.data as AgentTurnResult
    return { type: 'done', data: { ...data, workflow_progress: synthesizeWorkflowProgress(data) } }
  }
  if (event === 'error') return { type: 'error', data: parsed.data as { error: string } }
  return undefined
}

export const parseAgentSse = (value: string): AgentSseEvent[] => value.split(/\r?\n(?:\r?\n)+/).flatMap(block => {
  const event = parseEvent(block.trim())
  return event ? [event] : []
})

export type AgentTurn = {
  requestId: string
  idempotencyKey: string
  sessionId: string
  message: string
  context: AgentContext
  retry: () => AgentTurn
}

export const createAgentTurn = (sessionId: string, message: string, context: AgentContext, fixedRequestId?: string): AgentTurn => {
  const requestId = fixedRequestId || `agent_${crypto.randomUUID()}`
  const stableSession = sessionId || 'new'
  const turn = {
    requestId,
    idempotencyKey: `agent-${stableSession}-${requestId}`,
    sessionId,
    message,
    context,
    retry: () => turn,
  }
  return turn
}

const DSH_BRIDGE_URL = 'http://127.0.0.1:8890/turn'

// 非破坏的 DSH 开关：URL 带 ?brain=dsh 时 Agent 走 DSH 桥接（路 2），否则保持现有 Copilot。
export const brainMode = (): 'copilot' | 'dsh' => {
  if (typeof location === 'undefined') return 'copilot'
  return new URLSearchParams(location.search).get('brain') === 'dsh' ? 'dsh' : 'copilot'
}

async function streamDshTurn(turn: AgentTurn, signal: AbortSignal, onEvent: (event: AgentSseEvent) => void): Promise<void> {
  const response = await fetch(DSH_BRIDGE_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: turn.message, context: turn.context }),
    signal,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as Record<string, unknown>
    throw new Error(String(body.error || `DSH 请求失败 (${response.status})`))
  }
  const data = (await response.json().catch(() => ({}))) as { ok?: boolean; answer?: string; error?: string }
  if (!data.ok) throw new Error(String(data.error || 'DSH 返回失败'))
  const answer = data.answer || ''
  onEvent({ type: 'progress', data: { message: 'DSH 编排中…' } })
  onEvent({ type: 'text', data: { content: answer } })
  onEvent({ type: 'done', data: { session_id: turn.sessionId || 'dsh-session', answer, context: turn.context } })
}

export async function streamAgentTurn(turn: AgentTurn, signal: AbortSignal, onEvent: (event: AgentSseEvent) => void): Promise<void> {
  if (brainMode() === 'dsh') {
    await streamDshTurn(turn, signal, onEvent)
    return
  }
  const response = await fetch('/api/v1/copilot/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': turn.idempotencyKey },
    body: JSON.stringify({ request_id: turn.requestId, session_id: turn.sessionId, message: turn.message, context: turn.context }),
    signal,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as Record<string, unknown>
    throw new Error(String(body.detail || body.error || `Agent 请求失败 (${response.status})`))
  }
  if (!response.body) throw new Error('Agent 流式响应不可用')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    parseAgentSse(blocks.join('\n\n')).forEach(onEvent)
    if (done) break
  }
  parseAgentSse(buffer).forEach(onEvent)
}
