export type Job = {
  id: number; title: string; client: string; location?: string; status?: string;
  lifecycle_stage?: string; summary?: string; hard_requirements?: string;
  priority?: string; risk?: string; stop_condition?: string;
  candidate_count: number; active_candidate_count: number; updated_at?: string;
}

export type JobDetail = Job & {
  job_code?: string; source_layer?: string; ability_keywords?: string; target_companies?: string;
  exclusions?: string; search_words?: string; evidence_file?: string; closed_reason?: string; closed_at?: string;
  position: Record<string, any>;
  profile: Record<string, any>;
  funnel: { total: number; active: number; stopped: number; contacted: number; recommended: number };
  stages: Array<{ stage: string; count: number }>;
  candidates: Array<Candidate & { is_stopped?: boolean }>;
  search_experiments: Array<Record<string, any>>;
  events: Array<{ id: number; event_type: string; event_status?: string; event_time?: string; summary?: string; job_candidate_id?: number }>;
  followups: Array<Record<string, any>>;
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

export type Workflow = {
  ok: boolean;
  // 业务终态：null 或 completed_target_met / completed_needs_review / completed_pool_insufficient / failed_technical。
  // 后端在顶层、workflow、goal 三处冗余同一值；未知新值由 statusMapping 兜底，故类型保持宽松的 string。
  business_outcome?: string | null;
  goal: { title: string; objective: string; status: string; progress: number; started_at?: string; finished_at?: string; error?: string; business_outcome?: string | null; context?: { type?: string; id?: number; page?: string } };
  workflow: { workflow_id: string; status: string; current_stage?: string; updated_at?: string; started_at?: string; finished_at?: string; active_step_id?: number; archived_at?: string; business_outcome?: string | null };
  progress?: { completed: number; total: number; ratio: number };
  steps: Array<{
    id: number; sequence: number; business_label: string; reason?: string; risk_level: string; status: string;
    capability_id?: string; output?: Record<string, unknown>; output_json?: string; error?: string;
    verification?: { ok?: boolean; status?: string; summary?: string; checks?: Array<Record<string, unknown>> };
    recovery?: { action?: string; reason?: string; attempt?: number; max_attempts?: number };
    started_at?: string; finished_at?: string; updated_at?: string;
  }>;
  approvals: Array<{
    approval_id: string; title: string; risk_level: string; status: string; created_at: string; expires_at?: string;
    preflight?: { before?: string; after?: string; channel?: string; object_label?: string; action?: string };
  }>;
  artifacts: Array<{ artifact_id: string; title: string; artifact_type: string; validation_status: string }>;
  events?: Array<{ id: number; event_type: string; status: string; summary: string; created_at: string }>;
}

export type BusinessFocus = {
  session_id?: string; revision?: number; client?: string; action?: string; confidence?: number;
  context?: { type?: string; id?: number | null };
  job?: { id?: number; title?: string; status?: string };
  candidate?: { id?: number; name?: string };
  directions?: string[]; attachments?: string[]; constraints?: string[];
  conflicts?: Array<Record<string, unknown>>; needs_clarification?: boolean; updated_at?: string;
}

const json = async <T>(url: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  const body = await response.json().catch(() => ({ error: response.statusText }))
  if (!response.ok) throw new Error(body.detail || body.error || `请求失败 (${response.status})`)
  return body as T
}

export const api = {
  bootstrap: () => json<any>('/api/v1/bootstrap'),
  dashboard: () => json<any>('/api/v1/dashboard'),
  jobs: (q = '') => json<{items: Job[]; total: number}>(`/api/v1/jobs?limit=200&q=${encodeURIComponent(q)}`),
  job: (id: number) => json<{job: JobDetail}>(`/api/v1/jobs/${id}`),
  candidates: (q = '', jobId?: number) => json<{items: Candidate[]; total: number}>(`/api/v1/candidates?limit=200&q=${encodeURIComponent(q)}${jobId ? `&job_id=${jobId}` : ''}`),
  candidate: (id: number) => json<{candidate: CandidateDetail}>(`/api/v1/candidates/${id}`),
  workflow: (id: string) => json<Workflow>(`/api/v1/workflows/${encodeURIComponent(id)}`),
  copilot: (message: string, context: Record<string, unknown>, session_id = '') => write('/api/v1/copilot/messages', { message, context, session_id }),
  copilotSession: (sessionId: string) => json<{messages: Array<Record<string, any>>; business_focus?: BusinessFocus}>(`/api/agent/copilot/session?session_id=${encodeURIComponent(sessionId)}&limit=100`),
  workflowAction: (id: string, action: string, payload: Record<string, unknown> = {}) => write(`/api/v1/workflows/${id}/${action}`, payload),
  retryStep: (id: number) => json<any>(`/api/agent/steps/${id}/retry`, { method: 'POST', body: '{}' }),
  approval: (id: string, decision: string) => write(`/api/v1/approvals/${id}/decision`, { decision }),
  preflight: (candidate_id: number, action: string) => json<any>('/api/v1/candidate-actions/preflight', { method: 'POST', body: JSON.stringify({ request_id: requestId(), candidate_id, action }) }),
  commit: (candidate_id: number, action: string, preflight_token: string, note = '') => write('/api/v1/candidate-actions/commit', { candidate_id, action, preflight_token, note }),
}

const requestId = () => `web_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`
const write = (url: string, payload: Record<string, unknown>) => {
  const request_id = requestId()
  return json<any>(url, { method: 'POST', headers: { 'Idempotency-Key': `${request_id}_${url}` }, body: JSON.stringify({ ...payload, request_id }) })
}
