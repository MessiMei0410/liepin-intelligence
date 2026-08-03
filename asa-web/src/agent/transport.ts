import { z } from 'zod'

export type AgentContext = Record<string, unknown> & { type?: string; id?: string | number }

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
  business_focus?: Record<string, unknown> | null
  workflow_id?: string | null
  workflow_progress?: Record<string, unknown> | null
  pending_intent?: Record<string, unknown> | null
  action_card?: Record<string, unknown> | null
}

export type AgentSseEvent =
  | { type: 'context'; data: { session_id: string; context?: AgentContext; references?: AgentReference[]; suggested_actions?: Array<Record<string, unknown>> } }
  | { type: 'text'; data: { content: string } }
  | { type: 'done'; data: AgentTurnResult }
  | { type: 'error'; data: { error: string } }

const structuredRecord = z.record(z.string(), z.unknown())
const contextSchema = z.object({
  type: z.string().optional(), id: z.union([z.string(), z.number()]).optional(),
}).catchall(z.unknown())
const referenceSchema = z.object({
  type: z.string(), id: z.union([z.string(), z.number()]), label: z.string(),
  subtitle: z.string().optional(), href: z.string().optional(),
})
const contextEventSchema = z.object({
  session_id: z.string().min(1), context: contextSchema.optional(), references: z.array(referenceSchema).optional(),
  suggested_actions: z.array(structuredRecord).optional(),
})
const textEventSchema = z.object({ content: z.string() })
const doneEventSchema = z.object({
  ok: z.boolean().optional(), session_id: z.string().min(1), answer: z.string(), error: z.string().optional(), context: contextSchema.optional(),
  references: z.array(referenceSchema).optional(), suggested_actions: z.array(structuredRecord).optional(),
  business_focus: structuredRecord.nullable().optional(), workflow_id: z.string().nullable().optional(),
  workflow_progress: structuredRecord.nullable().optional(), pending_intent: structuredRecord.nullable().optional(),
  action_card: structuredRecord.nullable().optional(),
})
const errorEventSchema = z.object({ error: z.string() })

const parseEvent = (block: string): AgentSseEvent | undefined => {
  const lines = block.split(/\r?\n/)
  const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim()
  const rawData = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
  if (!event || !rawData) return undefined
  let value: unknown
  try { value = JSON.parse(rawData) } catch { return { type: 'error', data: { error: 'Agent 返回了无法解析的数据' } } }
  const parsed = event === 'context' ? contextEventSchema.safeParse(value)
    : event === 'text' ? textEventSchema.safeParse(value)
      : event === 'done' ? doneEventSchema.safeParse(value)
        : event === 'error' ? errorEventSchema.safeParse(value)
          : undefined
  if (!parsed) return undefined
  if (!parsed.success) return { type: 'error', data: { error: 'Agent 返回数据与约定格式不一致' } }
  if (event === 'context') return { type: 'context', data: parsed.data as Extract<AgentSseEvent, { type: 'context' }>['data'] }
  if (event === 'text') return { type: 'text', data: parsed.data as Extract<AgentSseEvent, { type: 'text' }>['data'] }
  if (event === 'done') return { type: 'done', data: parsed.data as AgentTurnResult }
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

export async function streamAgentTurn(turn: AgentTurn, signal: AbortSignal, onEvent: (event: AgentSseEvent) => void): Promise<void> {
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
