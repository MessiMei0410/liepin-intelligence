import type { components, paths } from './generated/api'
import { parseWorkflow } from './workflow/workflowModel'
import type { Workflow } from './workflow/workflowModel'
import { parseWorkflowCandidatesPage, parseWorkflowStepDetail, parseWorkflowSummary } from './workflow/workflowSummary'
import type { WorkflowCandidatesPage, WorkflowSummary } from './workflow/workflowSummary'

export type { Workflow } from './workflow/workflowModel'
export type { WorkflowCandidateItem, WorkflowCandidatesPage, WorkflowSummary } from './workflow/workflowSummary'

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

export type BusinessFocus = {
  session_id?: string; revision?: number; client?: string; action?: string; confidence?: number;
  context?: { type?: string; id?: number | null };
  job?: { id?: number; title?: string; status?: string };
  candidate?: { id?: number; name?: string };
  directions?: string[]; attachments?: string[]; constraints?: string[];
  conflicts?: Array<Record<string, unknown>>; needs_clarification?: boolean; updated_at?: string;
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
  paths['/api/v1/events']['get'],
  paths['/api/v1/candidate-actions/preflight']['post'],
  paths['/api/v1/candidate-actions/commit']['post'],
]
// 请求体引用生成的 components schema：Core 改字段（如 CandidateAction 增删属性）会在这里炸出类型错误。
type CandidateActionBody = components['schemas']['CandidateAction']
type CandidateActionRequest = Pick<CandidateActionBody, 'request_id' | 'candidate_id' | 'action'>

// 响应形状：Core 返回动态 dict（openapi 只描述为 object），按实际 payload 收窄声明；
// 工作流详情不在这里声明，由 workflowModel 的 zod schema 在边界校验（parseWorkflow）。
export type DashboardCounts = {
  active_jobs?: number; candidates?: number; pending_candidates?: number; pending_approvals?: number;
}
export type DashboardWorkflow = {
  workflow_id: string; status: string; title?: string; current_stage?: string; updated_at?: string; progress?: number;
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
export type CopilotResponse = Record<string, unknown> & { session_id?: string; business_focus?: BusinessFocus }
type WriteAck = Record<string, unknown> & { ok?: boolean }

const json = async <T>(url: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  const body: unknown = await response.json().catch(() => ({ error: response.statusText }))
  if (!response.ok) {
    const record = body && typeof body === 'object' ? body as Record<string, unknown> : {}
    throw new Error((record.detail || record.error || `请求失败 (${response.status})`) as string)
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
  copilot: (message: string, context: Record<string, unknown>, session_id = '') => write<CopilotResponse>('/api/v1/copilot/messages', { message, context, session_id }),
  copilotSession: (sessionId: string) => json<{messages: Array<Record<string, unknown>>; business_focus?: BusinessFocus}>(`/api/agent/copilot/session?session_id=${encodeURIComponent(sessionId)}&limit=100`),
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
const write = <T = WriteAck>(url: string, payload: Record<string, unknown>): Promise<T> => {
  const request_id = requestId()
  return json<T>(url, { method: 'POST', headers: { 'Idempotency-Key': `${request_id}_${url}` }, body: JSON.stringify({ ...payload, request_id }) })
}
