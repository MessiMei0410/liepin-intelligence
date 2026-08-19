import { z } from 'zod'

export type AgentContext = Record<string, unknown> & { type?: string; id?: string | number | null }

export type AgentReference = {
  type: string
  id: string | number
  label: string
  subtitle?: string
  href?: string
}

// DSH 子代理运行（subagent/subagent_fork 委派）：asa-server 把 Cordis subagent/start|end
// 透传为 SSE subagent 增量事件，transport 聚合成该数组挂到本轮 assistant 消息。
export type AgentSubagentRun = {
  id: string
  label: string
  status: 'running' | 'done' | 'failed' | 'stopped'
  summary?: string
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
  analysis_card?: Record<string, unknown> | null
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
  action_cards?: Array<Record<string, unknown>>
  // DSH 子代理运行终态（done 快照）：渲染「子代理执行」卡片并随 record-turn 回填。
  subagents?: AgentSubagentRun[]
  // DSH 写确认请求（preflight 申请投影）：前端渲染确认卡，用户确认后调 Core 激活+写入。
  confirm_request?: Record<string, unknown> | null
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
  | { type: 'thinking'; data: { content: string } }
  | { type: 'card'; data: Record<string, unknown> }
  | { type: 'confirm_request'; data: Record<string, unknown> }
  // DSH 子代理生命周期增量（asa-server SSE subagent 事件）：start/end 逐条到达，
  // transport 聚合进 done 的 subagents；流式期透传给上层做卡片实时状态更新。
  | { type: 'subagent'; data: { event: 'start' | 'end'; id: string; label?: string; status: AgentSubagentRun['status']; summary?: string } }
  // 本地合成事件（不经 SSE 解析）：done 后回填 Core 重试仍失败时由 streamDshTurn 发出，
  // 上层据此在消息流给用户一条可见提示（不阻断使用）。
  | { type: 'persist_failed'; data: { message: string } }
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
const cardEventSchema = structuredRecord
const subagentStatusSchema = z.enum(['running', 'done', 'failed', 'stopped'])
const subagentRunSchema = z.object({
  id: z.string().min(1), label: z.string().default(''), status: subagentStatusSchema,
  summary: z.string().nullish().transform(value => value || undefined),
})
const subagentEventSchema = z.object({
  event: z.enum(['start', 'end']), id: z.string().min(1),
  label: z.string().optional(), status: subagentStatusSchema,
  summary: z.string().nullish().transform(value => value || undefined),
})
const doneEventSchema = z.object({
  ok: z.boolean().optional(), session_id: z.string().min(1), answer: z.string(), error: z.string().optional(), context: contextSchema.optional(),
  references: z.array(referenceSchema).optional(), suggested_actions: z.array(structuredRecord).optional(),
  business_focus: structuredRecord.nullable().optional(), analysis_card: structuredRecord.nullable().optional(), workflow_id: z.string().nullable().optional(),
  workflow: structuredRecord.nullable().optional(), progress: structuredRecord.nullable().optional(),
  approvals: z.array(structuredRecord).optional(), plan_summary: z.array(structuredRecord).optional(),
  goal: structuredRecord.nullable().optional(),
  workflow_progress: structuredRecord.nullable().optional(), pending_intent: structuredRecord.nullable().optional(),
  action_card: structuredRecord.nullable().optional(),
  action_cards: z.array(structuredRecord).optional(),
  subagents: z.array(subagentRunSchema).optional(),
  confirm_request: structuredRecord.nullable().optional(),
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
      : event === 'thinking' ? textEventSchema.safeParse(value)
      : event === 'progress' ? progressEventSchema.safeParse(value)
        : event === 'card' ? cardEventSchema.safeParse(value)
          : event === 'confirm_request' ? cardEventSchema.safeParse(value)
            : event === 'subagent' ? subagentEventSchema.safeParse(value)
              : event === 'done' ? doneEventSchema.safeParse(value)
              : event === 'error' ? errorEventSchema.safeParse(value)
                : undefined
  if (!parsed) return undefined
  if (!parsed.success) return { type: 'error', data: { error: 'Agent 返回数据与约定格式不一致' } }
  if (event === 'context') return { type: 'context', data: parsed.data as Extract<AgentSseEvent, { type: 'context' }>['data'] }
  if (event === 'text') return { type: 'text', data: parsed.data as Extract<AgentSseEvent, { type: 'text' }>['data'] }
  if (event === 'thinking') return { type: 'thinking', data: parsed.data as Extract<AgentSseEvent, { type: 'thinking' }>['data'] }
  if (event === 'progress') return { type: 'progress', data: parsed.data as { message: string } }
  if (event === 'card') return { type: 'card', data: parsed.data as Record<string, unknown> }
  if (event === 'confirm_request') return { type: 'confirm_request', data: parsed.data as Record<string, unknown> }
  if (event === 'subagent') return { type: 'subagent', data: parsed.data as Extract<AgentSseEvent, { type: 'subagent' }>['data'] }
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
//（agent_copilot_messages rollup）并可刷新恢复。
// 调用方必须 await：上层 done 后立即 refreshSessions，若回填仍在飞行，列表会抢在
// 回填落库前返回——新会话在任务栏"消失"直到下次刷新（2026-08-19 验收 asa-9564e93f）。
// Core 重启/抖动时单次失败曾导致整段对话丢持久化（2026-08-19 dogfood P0）：改为
// 指数退避重试（默认共 3 次），且把非 2xx 响应当失败处理（此前只看网络异常）；
// 最终失败返回 false，由调用方发 persist_failed 事件在消息流给出可见提示。
export const DSH_RECORD_FAILED_NOTICE = '本轮对话内容未能保存到工作台，刷新后可能丢失本轮记录；可继续正常对话。'

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export async function recordDshTurn(
  turn: AgentTurn,
  data: Record<string, unknown>,
  { attempts = 3, baseDelayMs = 400 }: { attempts?: number; baseDelayMs?: number } = {},
): Promise<boolean> {
  // 写确认请求一并回填（注入本轮 request_id：恢复会话后确认/取消的终态
  // 回写需要它定位同一轮 assistant 消息）。
  const confirmRequest = data.confirm_request && typeof data.confirm_request === 'object'
    ? { ...(data.confirm_request as Record<string, unknown>), client_request_id: turn.requestId }
    : null
  const body = JSON.stringify({
    session_id: String(data.session_id || turn.sessionId),
    request_id: turn.requestId,
    message: turn.message,
    answer: String(data.answer || ''),
    context: turn.context,
    source: 'dsh',
    // 结构化卡片（名单卡等）一并回填：恢复会话时前端可重渲染卡片。
    ...(data.action_card && typeof data.action_card === 'object' ? { action_card: data.action_card } : {}),
    ...(Array.isArray(data.action_cards) && data.action_cards.length ? { action_cards: data.action_cards } : {}),
    // 子代理运行终态一并回填：恢复会话时「子代理执行」卡片可见终态。
    ...(Array.isArray(data.subagents) && data.subagents.length ? { subagents: data.subagents } : {}),
    // 轮末对象操作入口/对象卡一并回填：恢复会话时操作芯片仍可点击。
    ...(Array.isArray(data.suggested_actions) && data.suggested_actions.length ? { suggested_actions: data.suggested_actions } : {}),
    ...(Array.isArray(data.references) && data.references.length ? { references: data.references } : {}),
    ...(confirmRequest ? { confirm_request: confirmRequest } : {}),
    // Copilot 委托载荷（asa-server 并入 done）：理解卡/执行回执/焦点/模型参与/
    // 工作流进度卡一并回填，恢复会话时这些卡/条仍可重渲染。
    ...(data.understanding_card && typeof data.understanding_card === 'object' ? { understanding_card: data.understanding_card } : {}),
    ...(data.execution_receipt && typeof data.execution_receipt === 'object' ? { execution_receipt: data.execution_receipt } : {}),
    ...(data.analysis_card && typeof data.analysis_card === 'object' ? { analysis_card: data.analysis_card } : {}),
    ...(data.business_focus && typeof data.business_focus === 'object' ? { business_focus: data.business_focus } : {}),
    ...(data.model_participation && typeof data.model_participation === 'object' ? { model_participation: data.model_participation } : {}),
    ...(data.workflow_progress && typeof data.workflow_progress === 'object' ? { workflow_progress: data.workflow_progress } : {}),
    ...(typeof data.workflow_id === 'string' && data.workflow_id ? { workflow_id: data.workflow_id } : {}),
  })
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch('/api/v1/copilot/sessions/record-turn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      })
      if (response.ok) return true
      console.warn(`DSH 轮次回填 Core 返回 ${response.status}（第 ${attempt}/${attempts} 次）`)
    } catch (error) {
      console.warn(`DSH 轮次回填 Core 失败（第 ${attempt}/${attempts} 次）`, error)
    }
    if (attempt < attempts) await sleep(baseDelayMs * 2 ** (attempt - 1))
  }
  return false
}

// DSH 写确认终态回填：用户在确认卡点确认/取消后，按 (session_id, request_id)
// 把终态（confirmed/cancelled + 执行回执）回写同轮 assistant 消息——恢复会话时
// 确认卡呈现终态而非悬空的待确认。失败仅告警（本地卡片状态不受影响）。
export async function recordDshConfirmation(
  sessionId: string,
  clientRequestId: string,
  confirmResult: { state: 'confirmed' | 'cancelled'; summary?: string; execution_receipt?: Record<string, unknown> },
): Promise<void> {
  if (!sessionId || !clientRequestId) return
  try {
    await fetch('/api/v1/copilot/sessions/record-turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        request_id: clientRequestId,
        message: '',
        answer: '',
        context: {},
        source: 'dsh',
        confirm_result: confirmResult,
      }),
    })
  } catch (error) {
    console.warn('DSH 写确认终态回填失败（不影响本次写入结果）', error)
  }
}

// 非 completed 轮（aborted/超时/error，done.ok=false）回填：用户问句与已流式输出的
// 部分答案同样落库，刷新/掉线后不再整轮消失（dogfood P0-2：剧本 2 委托连续超时，
// turn 300s 被 abort，ok=false 跳过回填，reload 后问答丢失）。与 recordDshTurn 并列、
// 不改其重试语义；turn_error 记录中断原因供恢复时区分部分回答。
async function recordDshIncompleteTurn(turn: AgentTurn, data: Record<string, unknown>): Promise<void> {
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
        turn_error: String(data.error || 'turn did not complete'),
      }),
    })
  } catch (error) {
    console.warn('DSH 中断轮回填 Core 失败（不影响本轮结果）', error)
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
  // DSH 常驻服务器把工具结果里的 action_card / confirm_request 以独立事件透传（工具结果
  // 没有会话级归属字段，只能按序到达）。这里暂存并合并进 done：与 Copilot 脑一致在轮末
  // 挂到 assistant 消息（turn_done / 名单弹窗自动打开 / record-turn 回填同一条路径），
  // 不单独转发——上层事件循环只认 context/progress/text/thinking/done/error。
  let cardData: Record<string, unknown> | null = null
  let confirmRequestData: Record<string, unknown> | null = null
  // DSH 子代理运行聚合：SSE subagent 增量事件按 id 归并，流式期透传给上层实时更新卡片，
  // 轮末快照合并进 done（subagents），与 card/confirm_request 同一「拦截+并入 done」路径。
  const subagentRuns = new Map<string, AgentSubagentRun>()
  const track = (event: AgentSseEvent) => {
    if (event.type === 'card') {
      cardData = event.data
      return
    }
    if (event.type === 'confirm_request') {
      confirmRequestData = event.data
      return
    }
    if (event.type === 'subagent') {
      const { id } = event.data
      const run = subagentRuns.get(id) || { id, label: '', status: 'running' as const }
      if (event.data.label) run.label = event.data.label
      run.status = event.data.status
      if (event.data.summary) run.summary = event.data.summary
      subagentRuns.set(id, run)
      // 透传给上层：AgentWorkspace 显式处理 subagent 事件做卡片流式更新（不到 else 判失败分支）。
      onEvent(event)
      return
    }
    if (event.type === 'done') {
      const merged = { ...event.data }
      if (cardData && !merged.action_card) merged.action_card = cardData
      if (confirmRequestData && !merged.confirm_request) merged.confirm_request = confirmRequestData
      if (subagentRuns.size && !merged.subagents) merged.subagents = [...subagentRuns.values()]
      event.data = merged
      doneData = (event.data || {}) as Record<string, unknown>
    }
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
  // 回填先于 resolve：调用方随后 refreshSessions 时新会话必然已在列表里。
  // recordDshTurn 内部捕获全部异常，await 不会打断流式结果；重试后仍失败时
  // 发 persist_failed，由 AgentWorkspace 在本轮消息下渲染可见提示（不阻断使用）。
  // 非 completed 轮（aborted/超时）走 recordDshIncompleteTurn 同样回填——用户问句与
  // 部分答案不落库的话，刷新后整轮从会话里消失（dogfood P0-2）。
  if (doneData) {
    if ((doneData as { ok?: unknown }).ok !== false) {
      const persisted = await recordDshTurn(turn, doneData)
      if (!persisted) onEvent({ type: 'persist_failed', data: { message: DSH_RECORD_FAILED_NOTICE } })
    } else {
      await recordDshIncompleteTurn(turn, doneData)
    }
  }
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
