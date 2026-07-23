import type { components, paths } from './generated/api'
import { parseWorkflow } from './workflow/workflowModel'
import type { Workflow } from './workflow/workflowModel'
import { parseWorkflowCandidatesPage, parseWorkflowStepDetail, parseWorkflowSummary } from './workflow/workflowSummary'
import type { WorkflowCandidatesPage, WorkflowSummary } from './workflow/workflowSummary'
import { parseSourcingFunnel } from './workflow/sourcingFunnel'
import type { SourcingFunnel } from './workflow/sourcingFunnel'

export type { Workflow } from './workflow/workflowModel'
export type { WorkflowCandidateItem, WorkflowCandidatesPage, WorkflowSummary } from './workflow/workflowSummary'
export type { SourcingFunnel, SourcingFunnelChannel, SourcingFunnelRun } from './workflow/sourcingFunnel'

export type Job = {
  id: number; title: string; client: string; location?: string; status?: string;
  lifecycle_stage?: string; summary?: string; hard_requirements?: string;
  priority?: string; risk?: string; stop_condition?: string;
  candidate_count: number; active_candidate_count: number; updated_at?: string;
}

export type JobDetail = Job & {
  job_code?: string; source_layer?: string; ability_keywords?: string; target_companies?: string;
  exclusions?: string; search_words?: string; evidence_file?: string; closed_reason?: string; closed_at?: string;
  position: Record<string, unknown>;
  profile: Record<string, unknown>;
  funnel: { total: number; active: number; stopped: number; contacted: number; recommended: number };
  stages: Array<{ stage: string; count: number }>;
  candidates: Array<Candidate & { is_stopped?: boolean }>;
  search_experiments: Array<Record<string, unknown> & { id?: string | number }>;
  events: Array<{ id: number; event_type: string; event_status?: string; event_time?: string; summary?: string; job_candidate_id?: number }>;
  followups: Array<Record<string, unknown> & { id?: string | number }>;
  next_keywords?: string[]; metric_target_companies?: string[]; exclude_terms?: string[];
}

export type Candidate = {
  id: number; person_id: number; name: string; current_company?: string; current_title?: string;
  city?: string; education?: string; experience?: string; job_id?: number; job?: string; client?: string;
  clean_stage?: string; flow_bucket?: string; raw_status?: string; updated_at?: string; source_type?: string;
}

export type CandidateDetail = Candidate & {
  is_stopped: boolean; stop_reason?: string;
  // R10：停止原因枚举回显，code 为 8 枚举之一（未知值后端降级 other），label 为中文文案。
  stop_reason_code?: string; stop_reason_label?: string;
  resume: { summary: string; full_text: string; work_text: string; project_text: string; education_text: string; raw: Record<string, unknown> };
  source_links: Array<{ source_system: string; source_entity_type: string; source_entity_id: string; source_url?: string }>;
  events: Array<{ id: number; event_type: string; event_status?: string; event_time?: string; summary?: string }>;
  job_relations: Array<{ id: number; job_id: number; job: string; client: string; clean_stage?: string; flow_bucket?: string; updated_at?: string }>;
  sourcing_attributions: Array<{
    id: number; channel: string; source_query: string; source_round?: string; source_purpose?: string;
    workflow_id?: string; strategy_model?: string; learning_score: number; signal_count: number;
    review_pass_count: number; contacted_count: number; recommended_count: number; stopped_count: number;
    client_positive_count: number; client_rejected_count: number;
  }>;
}

// R5 typed client：dashboard / workflow / candidate-action 三个高频接口全链路类型化。
// R7 增补：summary / steps / candidates 增量路由与 SSE events 路由同样入锚。
// 契约锚点：Core 重命名或下线下列路由时 typecheck 直接报错（仅编译期，无运行时开销）。
export type ContractAnchor = [
  paths['/api/v1/dashboard']['get'],
  paths['/api/v1/workflows/{workflow_id}']['get'],
  paths['/api/v1/workflows/{workflow_id}/summary']['get'],
  paths['/api/v1/workflows/{workflow_id}/steps/{step_id}']['get'],
  paths['/api/v1/workflows/{workflow_id}/candidates']['get'],
  // R8 增补：渠道寻访漏斗路由入锚。
  paths['/api/v1/workflows/{workflow_id}/sourcing-funnel']['get'],
  paths['/api/v1/events']['get'],
  paths['/api/v1/candidate-actions/preflight']['post'],
  paths['/api/v1/candidate-actions/commit']['post'],
  // R10 增补：停止原因摘要路由入锚。
  paths['/api/v1/candidates/stop-reasons/summary']['get'],
  // S4-3 增补：策略复盘读取与按需重算路由入锚。
  paths['/api/v1/workflows/{workflow_id}/strategy-review']['get'],
  paths['/api/v1/workflows/{workflow_id}/strategy-review/rebuild']['post'],
  // S4-3c 增补：逐项决策回写路由入锚。generated/api.d.ts 尚无此路由（后端本期新上，
  // 主控 regenerate 后条件类型自动收紧为真实 patch 操作类型，此前解析为 never）。
  StrategyReviewDiffsPatchAnchor,
]
// 请求体引用生成的 components schema：Core 改字段（如 CandidateAction 增删属性）会在这里炸出类型错误。
type CandidateActionBody = components['schemas']['CandidateAction']
type CandidateActionRequest = Pick<CandidateActionBody, 'request_id' | 'candidate_id' | 'action'>

// S4-3c PATCH diffs 锚点：生成类型里有了该路由即生效，没有则为 never（不阻断 typecheck）。
type RoutePatchOp<P, K extends PropertyKey> = K extends keyof P ? (P[K] extends { patch: infer R } ? R : never) : never
type StrategyReviewDiffsPatchAnchor = RoutePatchOp<paths, '/api/v1/workflows/{workflow_id}/strategy-review/diffs'>

// 响应形状：Core 返回动态 dict（openapi 只描述为 object），按实际 payload 收窄声明；
// 工作流详情不在这里声明，由 workflowModel 的 zod schema 在边界校验（parseWorkflow）。
export type DashboardCounts = {
  active_jobs?: number; candidates?: number; pending_candidates?: number; pending_approvals?: number;
}
export type DashboardWorkflow = {
  workflow_id: string; status: string; business_outcome?: string | null; title?: string; current_stage?: string; updated_at?: string; progress?: number;
}
export type Dashboard = {
  ok?: boolean; counts?: DashboardCounts; workflows?: DashboardWorkflow[];
  recent_events?: Array<Record<string, unknown>>;
}
export type Bootstrap = {
  ok?: boolean;
  core?: { status?: string; db?: string; api_version?: string };
  user?: { id?: string; name?: string };
  counts?: DashboardCounts; features?: Record<string, boolean>;
}
export type PreflightResult = { token: string; impact: string; expires_at?: string }
type WriteAck = Record<string, unknown> & { ok?: boolean }

// S4-3 策略复盘：Core 返回动态 dict（openapi 只描述为 object），按 strategy_review.py 实际 payload 收窄声明。
// revision_diff 逐项可采纳/拒绝（status: pending → accepted/rejected）；S4-3c 起决策经
// PATCH /strategy-review/diffs 回写后端持久化（并写 consultant_edits / explicit_corrections
// 学习信号），localStorage 仅作 API 失败时的缓存回退。
export type StrategyReviewDiff = {
  diff_id: string
  step: string
  op: 'add' | 'replace' | 'review'
  tier?: string
  companies?: string[]
  group?: string
  terms?: string[]
  reason: string
  status: 'pending' | 'accepted' | 'rejected'
  decided_at?: string
}
// S4-3c-3（N3）池枯竭信号与扩池决策树：dedupe_rate > 阈值（0.80，可配置）触发 pool_saturated
// 信号，并按序输出固定 5 步扩池决策树（order 即执行优先级）。树决策本期无后端回写接口
// （PATCH /strategy-review/diffs 仅收 revision_diff 条目），前端逐项采纳/拒绝走 localStorage，
// revise 提交时以 "【采纳步骤】…【拒绝步骤】…" 后缀并入 instruction。
export type StrategyReviewSignal = {
  signal: string
  label?: string
  scope?: string
  dedupe_rate?: number | null
  dedupe_count?: number
  extracted_count?: number
  threshold?: number
  channels?: Array<{ channel?: string; extracted_count?: number; dedupe_count?: number; dedupe_rate?: number | null }>
  detail?: string
  semantics?: string
}
export type ExpansionActionType = 'swap_keywords' | 'expand_pool' | 'relax_condition' | 'rebalance_channel' | 'escalate_mapping'
export type ExpansionKeywordGroup = { group?: string; targets?: string; terms?: string[] }
export type ExpansionRelaxItem = { field?: string; current?: string | string[] | null; proposal?: string; cost?: string; note?: string; source?: string }
export type ExpansionChannelStat = { channel?: string; recall_count?: number; unique_count?: number; intake_new_count?: number; intake_conversion?: number | null }
// params 为 5 种 action_type 各自形状的并集（后端只取真实值，取不到留空）；缺省键前端如实显示"待顾问补充"。
export type ExpansionTreeParams = {
  current_groups?: ExpansionKeywordGroup[]; candidate_groups?: ExpansionKeywordGroup[]; rotation?: string
  current_tiers?: string[]; next_tier?: string | null; tier_label?: string; companies?: string[]; rationale?: string; source_archetype?: string
  items?: ExpansionRelaxItem[]; boundary?: string
  channel_stats?: ExpansionChannelStat[]; recommended_channel?: string | null; basis?: string
  actions?: string[]; reason?: string
}
export type ExpansionTreeStep = {
  step_id: string
  order?: number
  action_type: ExpansionActionType
  title?: string
  detail?: string
  params?: ExpansionTreeParams
  status?: string
}
export type StrategyReviewChannelFinding = {
  channel: string; status?: string; recall_count?: number; unique_count?: number;
  intake_new_count?: number; assessed_count?: number; high_score_count?: number;
  detail_complete?: number; detail_partial?: number; detail_failed?: number;
  detail_failed_ratio?: number | null; zero_attribution?: string | null;
  finding?: string; note?: string;
}
export type StrategyReviewEvidence = {
  has_strategy_v2?: boolean; funnel_channels?: number; expected_recall_total?: number;
  recall_total?: number; detail_total?: number; detail_failed_total?: number;
  detail_failed_ratio?: number | null; intake_new_total?: number; assessed_total?: number;
  high_score_total?: number; high_score_rate?: number | null; assessment_source?: string;
}
export type StrategyReview = {
  verdict: string; verdict_label: string; verdict_reason: string; degraded?: boolean;
  thresholds?: { recall_shortfall_ratio?: number; detail_failed_ratio?: number; high_score_rate?: number };
  evidence?: StrategyReviewEvidence;
  per_channel_findings?: StrategyReviewChannelFinding[];
  revision_diff?: StrategyReviewDiff[];
  signals?: StrategyReviewSignal[];
  expansion_decision_tree?: ExpansionTreeStep[];
  escalation?: { kind?: string; target?: string; reason?: string; status?: string } | null;
  notes?: string[]; generated_at?: string; version?: number;
  history?: Array<{ version?: number; verdict?: string; verdict_reason?: string; generated_at?: string }>;
}
export type StrategyReviewPayload = {
  ok?: boolean; artifact_id: string; workflow_id: string; title: string; content: string;
  created_at?: string; review: StrategyReview;
}
export type StrategyReviewRebuildResult = { ok?: boolean; workflow_id: string; artifact_id: string; review: StrategyReview }

// S4-3c 逐项决策回写：generated/api.d.ts 尚无 PATCH /strategy-review/diffs（后端本期新上，主控
// regenerate 后切换为生成类型），先按 strategy_review.apply_diff_decisions 实际 payload 窄声明。
export type StrategyReviewDiffDecision = { diff_id: string; status: 'accepted' | 'rejected' }
export type StrategyReviewDiffPatchResult = {
  ok?: boolean; workflow_id: string; artifact_id: string; updated: number;
  revision_diff: StrategyReviewDiff[];
  consultant_edits_appended?: number; learning_signal_recorded?: boolean;
}

const json = async <T>(url: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  const body: unknown = await response.json().catch(() => ({ error: response.statusText }))
  if (!response.ok) {
    const record = body && typeof body === 'object' ? body as Record<string, unknown> : {}
    // 携带 HTTP status：调用方需区分 409（状态漂移/签名不符）等可读错误。
    const error = new Error((record.detail || record.error || `请求失败 (${response.status})`) as string) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  return body as T
}

export const api = {
  bootstrap: () => json<Bootstrap>('/api/v1/bootstrap'),
  dashboard: () => json<Dashboard>('/api/v1/dashboard'),
  jobs: (q = '') => json<{items: Job[]; total: number}>(`/api/v1/jobs?limit=200&q=${encodeURIComponent(q)}`),
  job: (id: number) => json<{job: JobDetail}>(`/api/v1/jobs/${id}`),
  candidates: (q = '', jobId?: number) => json<{items: Candidate[]; total: number}>(`/api/v1/candidates?limit=200&q=${encodeURIComponent(q)}${jobId ? `&job_id=${jobId}` : ''}`),
  candidate: (id: number) => json<{candidate: CandidateDetail}>(`/api/v1/candidates/${id}`),
  workflow: async (id: string): Promise<Workflow> => parseWorkflow(await json<unknown>(`/api/v1/workflows/${encodeURIComponent(id)}`)),
  // R7 增量路由：轮询打 summary，步骤完整 output 与人选列表按需分页拉取。
  workflowSummary: async (id: string): Promise<WorkflowSummary> => parseWorkflowSummary(await json<unknown>(`/api/v1/workflows/${encodeURIComponent(id)}/summary`)),
  workflowStep: async (id: string, stepId: number): Promise<Workflow['steps'][number]> => {
    const detail = parseWorkflowStepDetail(await json<unknown>(`/api/v1/workflows/${encodeURIComponent(id)}/steps/${stepId}`))
    if (!detail.step) throw new Error('步骤详情响应不完整')
    return detail.step
  },
  workflowCandidates: async (id: string, limit = 20, offset = 0): Promise<WorkflowCandidatesPage> =>
    parseWorkflowCandidatesPage(await json<unknown>(`/api/v1/workflows/${encodeURIComponent(id)}/candidates?limit=${limit}&offset=${offset}`)),
  // R8 渠道漏斗：独立按需路由（面板挂载/详情刷新时拉取），不进 /summary 轮询签名，轮询负载不回升。
  workflowSourcingFunnel: async (id: string): Promise<SourcingFunnel> =>
    parseSourcingFunnel(await json<unknown>(`/api/v1/workflows/${encodeURIComponent(id)}/sourcing-funnel`)),
  // S4-3 策略复盘：无复盘或工作流不存在时 Core 返回 404，此处收敛为 null（其余错误照常抛出，携带 status）。
  strategyReview: async (id: string): Promise<StrategyReviewPayload | null> => {
    try {
      return await json<StrategyReviewPayload>(`/api/v1/workflows/${encodeURIComponent(id)}/strategy-review`)
    } catch (error) {
      if ((error as { status?: number }).status === 404) return null
      throw error
    }
  },
  // 按需重算（终局工作流补生成）：幂等走 write 封装；非终局 Core 返回 409，错误带 status 由调用方呈现。
  rebuildStrategyReview: (id: string) =>
    write<StrategyReviewRebuildResult>(`/api/v1/workflows/${encodeURIComponent(id)}/strategy-review/rebuild`, {}),
  // S4-3c 逐项采纳/拒绝回写：upsert 可重复覆盖，与 revise 各自幂等（不同 Idempotency-Key）。
  // 404=工作流/复盘不存在，409=diff_id 未知或状态非法；错误带 status 由调用方决定降级策略。
  patchStrategyReviewDiffs: (id: string, decisions: StrategyReviewDiffDecision[]) =>
    write<StrategyReviewDiffPatchResult>(`/api/v1/workflows/${encodeURIComponent(id)}/strategy-review/diffs`, { decisions }, 'PATCH'),
  workflowAction: (id: string, action: string, payload: Record<string, unknown> = {}) => write(`/api/v1/workflows/${id}/${action}`, payload),
  retryStep: (id: number) => json<WriteAck>(`/api/agent/steps/${id}/retry`, { method: 'POST', body: '{}' }),
  approval: (id: string, decision: string) => write(`/api/v1/approvals/${id}/decision`, { decision }),
  preflight: (candidate_id: number, action: string) => {
    const body: CandidateActionRequest = { request_id: requestId(), candidate_id, action }
    return json<PreflightResult>('/api/v1/candidate-actions/preflight', { method: 'POST', body: JSON.stringify(body) })
  },
  commit: (candidate_id: number, action: string, preflight_token: string, note = '', reason?: string) => {
    const body: Omit<CandidateActionBody, 'request_id' | 'reason'> & { reason?: string } = { candidate_id, action, preflight_token, note, ...(reason ? { reason } : {}) }
    return write('/api/v1/candidate-actions/commit', body)
  },
}

const requestId = () => `web_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`
const write = <T = WriteAck>(url: string, payload: Record<string, unknown>, method: 'POST' | 'PATCH' = 'POST'): Promise<T> => {
  const request_id = requestId()
  return json<T>(url, { method, headers: { 'Idempotency-Key': `${request_id}_${url}` }, body: JSON.stringify({ ...payload, request_id }) })
}
