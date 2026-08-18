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

const DSH_BRIDGE_URL = 'http://127.0.0.1:8891/turn'

// DSH 默认大脑（2026-08-18 起）：Agent 默认走 DSH 常驻服务器（路 2 编排层）；
// URL 带 ?brain=copilot 可临时回退到 Python Copilot 直连。
export const brainMode = (): 'copilot' | 'dsh' => {
  if (typeof location === 'undefined') return 'dsh'
  return new URLSearchParams(location.search).get('brain') === 'copilot' ? 'copilot' : 'dsh'
}

// DSH 桥接配置（token + url）：从 Core 拉取，成功才缓存；失败按 dev 回退（无 token + 默认 url）
// 但不缓存负结果——否则 Core 未就绪时一次失败会导致整页生命周期内 401，必须刷新才能恢复。
let dshConfigCache: { token: string; url: string } | null = null

async function getDshConfig(): Promise<{ token: string; url: string }> {
  if (dshConfigCache) return dshConfigCache
  try {
    const response = await fetch('/api/v1/dsh-config')
    if (response.ok) {
      const data = (await response.json().catch(() => ({}))) as { token?: string; url?: string }
      dshConfigCache = { token: data.token || '', url: data.url || DSH_BRIDGE_URL }
      return dshConfigCache
    }
  } catch {
    // 忽略，走回退
  }
  return { token: '', url: DSH_BRIDGE_URL }
}

// DSH 轮次回填 Core：DSH 对话只存在其服务器内存，回填后才会进入会话列表
//（agent_copilot_messages rollup）并可刷新恢复。失败仅告警，绝不影响流式主流程。
async function recordDshTurn(turn: AgentTurn, data: Record<string, unknown>): Promise<void> {
  try {
    await fetch('/api/v1/copilot/sessions/record-turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: String(data.session_id || turn.sessionId),
        request_id: turn.requestId,
        message: turn.message,
        answer: String(data.answer || ''),
        context: turn.context,
        source: 'dsh',
      }),
    })
  } catch (error) {
    console.warn('DSH 轮次回填 Core 失败（不影响本轮结果）', error)
  }
}

async function streamDshTurn(turn: AgentTurn, signal: AbortSignal, onEvent: (event: AgentSseEvent) => void): Promise<void> {
  const { token, url } = await getDshConfig()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({ message: turn.message, session_id: turn.sessionId, context: turn.context }),
    signal,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as Record<string, unknown>
    throw new Error(String(body.error || `DSH 请求失败 (${response.status})`))
  }
  if (!response.body) throw new Error('DSH 流式响应不可用')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let doneData: Record<string, unknown> | null = null
  const track = (event: AgentSseEvent) => {
    if (event.type === 'done') doneData = (event.data || {}) as Record<string, unknown>
    onEvent(event)
  }
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    parseAgentSse(blocks.join('\n\n')).forEach(track)
    if (done) break
  }
  parseAgentSse(buffer).forEach(track)
  if (doneData && (doneData as { ok?: unknown }).ok !== false) void recordDshTurn(turn, doneData)
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
