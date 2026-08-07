import type { components, paths } from './generated/api'
import { parseWorkflow } from './workflow/workflowModel'
import type { Workflow } from './workflow/workflowModel'
import { parseWorkflowCandidatesPage, parseWorkflowStepDetail, parseWorkflowSummary } from './workflow/workflowSummary'
import type { WorkflowCandidatesPage, WorkflowSummary } from './workflow/workflowSummary'
import { parseSourcingFunnel } from './workflow/sourcingFunnel'
import type { SourcingFunnel } from './workflow/sourcingFunnel'
import { parseAgentSession, parseAgentSessionList, parseAgentSessionUpdate } from './agent/sessionModel'

export type { Workflow } from './workflow/workflowModel'
export type { WorkflowCandidateItem, WorkflowCandidatesPage, WorkflowSummary } from './workflow/workflowSummary'
export type { SourcingFunnel, SourcingFunnelChannel, SourcingFunnelRun } from './workflow/sourcingFunnel'
export type { AgentMessage, AgentSession, AgentSessionSummary } from './agent/sessionModel'

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
  latest_effective_strategy?: {
    status: string; plan_version: number; generated_at?: string; summary?: string; input_level?: string;
    company_tiers: Array<{ path?: string; tier?: string; companies: string[]; rationale?: string }>;
    level_mapping: Record<string, unknown>;
    keyword_groups: Array<{ group?: string; targets?: string; terms: string[] }>;
    expectation: Record<string, unknown>;
    consultant_constraints: Array<{ type: string; rule: string }>;
    audit: { workflow_id?: string; artifact_id?: string; schema_version?: string };
  } | null;
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
  report_artifacts?: Array<{
    id: number; artifact_id: string; workflow_id?: string; artifact_type: string; title: string;
    mime_type?: string; validation_status: string; created_at?: string; version: number;
  }>;
  // 版本化推荐包摘要（顾问确认推荐后生成；无确认推荐为空数组，前端不渲染区块）。
  recommendation_packages?: RecommendationPackageSummary[];
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
  paths['/api/v1/artifacts/{artifact_id}']['get'],
  paths['/api/v1/artifacts/{artifact_id}/file']['get'],
  paths['/api/v1/analytics/runs/{run_id}/download']['get'],
  // R8 增补：渠道寻访漏斗路由入锚。
  paths['/api/v1/workflows/{workflow_id}/sourcing-funnel']['get'],
  paths['/api/v1/events']['get'],
  paths['/api/v1/candidate-actions/preflight']['post'],
  paths['/api/v1/candidate-actions/commit']['post'],
  paths['/api/v1/agent/metrics']['get'],
  paths['/api/v1/copilot/sessions']['get'],
  paths['/api/v1/copilot/sessions/{session_id}']['get'],
  paths['/api/v1/copilot/stream']['post'],
  paths['/api/v1/workbench']['get'],
  paths['/api/v1/analytics/runs']['post'],
  paths['/api/v1/analytics/runs/{run_id}']['get'],
  paths['/api/v1/analytics/runs/{run_id}/refresh']['post'],
  paths['/api/v1/analytics/templates']['get'],
  // R10 增补：停止原因摘要路由入锚。
  paths['/api/v1/candidates/stop-reasons/summary']['get'],
  // S4-3 增补：策略复盘读取与按需重算路由入锚。
  paths['/api/v1/workflows/{workflow_id}/strategy-review']['get'],
  paths['/api/v1/workflows/{workflow_id}/strategy-review/rebuild']['post'],
  // S4-3c 增补：逐项决策回写路由入锚。generated/api.d.ts 尚无此路由（后端本期新上，
  // 主控 regenerate 后条件类型自动收紧为真实 patch 操作类型，此前解析为 never）。
  StrategyReviewDiffsPatchAnchor,
  // S5-2 增补：Mapping 任务卡读取/创建路由入锚（S5-1 已在生成类型中）。
  // PATCH /mapping-tasks/{artifact_id}/candidates/{index} 与 icebreaker/intake 两个 POST 为
  // S5-2 后端新上，generated/api.d.ts 尚无，待主控 regenerate 后补锚。
  paths['/api/v1/jobs/{job_id}/mapping-tasks/{artifact_id}']['get'],
  paths['/api/v1/jobs/{job_id}/mapping-tasks']['post'],
  // S8 增补：岗位画像读取/顾问纠正路由入锚（generated/api.d.ts 已 regenerate）。
  paths['/api/v1/jobs/{job_id}/profile-insights']['get'],
  paths['/api/v1/jobs/{job_id}/profile-insights/feedback']['post'],
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
  active_jobs?: number; candidates?: number; pending_candidates?: number; pending_approvals?: number; pending_proposals?: number; executed_proposals?: number; failed_proposals?: number;
}
export type DashboardWorkflow = {
  workflow_id: string; status: string; business_outcome?: string | null; title?: string; current_stage?: string; updated_at?: string; progress?: number;
}
export type Dashboard = {
  ok?: boolean; counts?: DashboardCounts; workflows?: DashboardWorkflow[];
  recent_events?: Array<Record<string, unknown>>;
}
export type ModelAuditCall = {
  call_id: string; operation: string; provider: string; model: string;
  status: string; validation_status: string; fallback_used: number;
  duration_ms: number; input_tokens: number; output_tokens: number;
  request_hash: string; request_preview: string; response_preview: string;
  error?: string | null; created_at: string; finished_at?: string | null;
}
export type ModelAuditResponse = {
  ok: boolean;
  items: ModelAuditCall[];
  summary: { total: number; failed: number; fallback: number; avg_duration_ms: number };
}
export type Bootstrap = {
  ok?: boolean;
  core?: { status?: string; db?: string; api_version?: string };
  user?: { id?: string; name?: string };
  counts?: DashboardCounts; features?: Record<string, boolean>;
}

export type AnalysisMetric = {
  id: string; label: string; value: number | null; unit: string;
  definition_id: string; definition_version: string;
  // 交付记分卡等目录附加的样本量与口径说明；旧目录项不下发这两个字段。
  sample_size?: number | null; note?: string;
}
export type AnalysisReference = {
  type: 'job' | 'candidate' | 'workflow' | string; id: string | number; label: string; href?: string;
}
export type AnalysisSection = {
  type: 'table' | 'funnel' | 'bar' | 'trend' | 'candidate_list'; title: string;
  columns: string[]; rows: Array<Record<string, unknown>>;
}
export type AnalysisResult = {
  schema_version: 'analysis_result_v1'; run_id: string; catalog_id: string; catalog_version: string;
  status: 'completed' | 'partial' | 'failed' | 'expired'; question: string; scope: Record<string, unknown>;
  data_as_of: string; headline: string; metrics: AnalysisMetric[]; sections: AnalysisSection[];
  references: AnalysisReference[]; caveats: string[]; truncated: boolean;
  suggested_actions: Array<Record<string, unknown>>; supersedes_run_id?: string | null;
}
export type AnalysisTemplate = {
  template_id: string; name: string; catalog_id: string; question: string; scope: Record<string, unknown>;
  enabled: boolean; schedule_kind: 'manual' | 'daily' | 'weekly'; schedule_enabled: boolean;
  schedule_time: string; schedule_weekday: number; timezone: string;
  next_run_at?: string | null; last_run_at?: string | null; last_status?: 'running' | 'completed' | 'failed' | 'skipped' | null;
  last_run_id?: string | null; last_result?: AnalysisResult | null; created_at?: string; updated_at?: string;
}
export type AnalysisCatalogItem = {
  catalog_id: string; label: string; allowed_scope_fields: string[];
}
export type AnalysisTemplateInput = {
  name: string; catalog_id: string; question: string; scope: Record<string, unknown>;
  schedule_kind: AnalysisTemplate['schedule_kind']; schedule_enabled: boolean;
  schedule_time: string; schedule_weekday: number; timezone: string;
}
export type AnalysisTemplateRun = {
  template_run_id: string; template_id: string; analysis_run_id?: string | null;
  trigger: 'manual' | 'schedule'; status: 'running' | 'completed' | 'failed' | 'skipped';
  started_at: string; completed_at?: string | null; error?: string | null; headline?: string | null; data_as_of?: string | null;
}
export type AnalysisTrendPoint = { run_id: string; at: string; value: number | null }
export type AnalysisTrendSeries = {
  metric_id: string; label: string; unit: string; latest: number | null; previous: number | null;
  delta: number | null; delta_ratio: number | null; points: AnalysisTrendPoint[];
}
export type AnalysisTrend = {
  ok: boolean; template_id: string; name: string; catalog_id: string; run_count: number;
  runs: Array<{ run_id: string; at: string; headline?: string; values: Record<string, number | null> }>;
  series: AnalysisTrendSeries[];
}
export type WorkbenchAction = {
  type: 'open_candidate' | 'open_workflow' | 'open_analysis'; id: string; label: string;
}
// 工作台五分组：decision 待判断 / running 运行中 / waiting_client 待客户 / risk 风险/逾期 / delivered 最近交付。
// 'pending' 仅作历史兼容保留在联合类型里，服务端自五分组起不再下发。
export type WorkbenchLane = 'decision' | 'running' | 'waiting_client' | 'risk' | 'delivered'
export type WorkbenchItem = {
  item_key: string; source_revision: string; kind: 'candidate_action' | 'approval' | 'analysis' | 'workflow';
  lane: WorkbenchLane | 'pending'; priority_score: number; title: string; subtitle: string;
  status_label: string; reason: string; source_label: string; updated_at?: string;
  inbox_state: 'unread' | 'read' | 'later' | 'hidden'; primary_action: WorkbenchAction;
}
export type WorkbenchSummary = {
  pending: number; running: number; delivered: number; total: number;
  decision?: number; waiting_client?: number; risk?: number;
}
export type Workbench = {
  ok: boolean; version: string; summary: WorkbenchSummary;
  items: WorkbenchItem[]; returned_count?: number; truncated?: boolean;
}
// summary.pending 是 decision 的兼容别名；新 lane 计数缺失时回落（decision→pending，其余→0）。
export const workbenchLaneCount = (workbench: Workbench, lane: WorkbenchLane): number =>
  lane === 'decision' ? (workbench.summary.decision ?? workbench.summary.pending) : (workbench.summary[lane] ?? 0)
// 工作台窗口契约：/api/v1/workbench 接受 1..1000 的 limit；服务端序列化窗口统一封顶 300
// （analytics.workbench），超出部分由 truncated/returned_count 显式告知，summary 仍按
// flow 全量可见项（最多 1000）统计。请求端固定拉取声明上限，让截断只由服务端真实数据触发。
export const WORKBENCH_LIMIT = 1000
export type PreflightResult = { token: string; impact: string; expires_at?: string }
type WriteAck = Record<string, unknown> & { ok?: boolean }
export type AgentProposal = {
  proposal_id: string; status: 'pending' | 'approved' | 'rejected' | 'executed' | 'failed';
  action_type: string; risk_level: string; title: string; rationale?: string; candidate?: string;
  company?: string; candidate_title?: string; client?: string; job?: string; request?: Record<string, unknown>;
  preflight?: Record<string, unknown>; expires_at?: string;
}
export type AgentProposalPreflight = {
  ok: boolean; proposal_id: string; action_type: string; request: Record<string, unknown>;
  policy: { decision?: string; risk_level?: string; reason?: string }; confirmation_token: string; expires_in: number;
}
export type AgentActionMetrics = {
  action_cards_generated: number; confirmed: number; rejected: number; executed: number; failed: number; needs_clarification: number;
  confirmation_rate: number | null; rejection_rate: number | null; execution_failure_rate: number | null;
  r3_approvals: { total: number; approved: number; approval_rate: number | null };
}
export type FloatingActiveContext = {
  surface?: string; source_label?: string; type?: string; id?: string | number | null;
  title?: string; subtitle?: string; status?: string; connected?: boolean; stopped?: boolean;
  job_candidate_id?: number | null; updated_at?: string; age_seconds?: number; stale?: boolean;
}
export type FloatingSuggestedAction = {
  id: string; type?: string; title?: string; detail?: string; kind?: string;
}
export type FloatingBridgeContext = {
  surface?: string; context_key?: string; instance_id?: string; job_candidate_id?: number | null;
  title?: string; subtitle?: string; updated_at?: string;
}
export type FloatingStatePayload = {
  ok?: boolean; server_time?: string; active_context?: FloatingActiveContext; active_context_raw?: Record<string, unknown>;
  context_quality?: Record<string, unknown>; diagnostics?: Array<Record<string, unknown>>;
  suggested_actions?: FloatingSuggestedAction[];
}
export type FloatingActionResult = WriteAck & {
  status?: string; message?: string; open_url?: string; workflow_id?: string;
  command?: Record<string, unknown>; result?: Record<string, unknown>; workflow?: Record<string, unknown>;
}
export type AgentCandidateAssessResult = WriteAck & {
  status?: string; message?: string; assessment_id?: number; job_candidate_id?: number; run_id?: string;
  cached?: boolean; coalesced?: boolean; error?: string;
  result?: Record<string, unknown>;
}
export type AgentRunResult = AgentCandidateAssessResult & { updated_at?: string }
export type CandidateActionResult = WriteAck & {
  candidate_id?: number; action?: string; stage?: string; already_applied?: boolean;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}
export type ConsultantRecommendationPreflight = PreflightResult & {
  action?: 'consultant_recommendation'; already_confirmed?: boolean; confirmed_at?: string | null;
}
export type ConsultantRecommendationResult = WriteAck & {
  candidate_id?: number; action?: string; reason?: string; already_confirmed?: boolean;
  confirmed_at?: string; event_id?: number; business_event_id?: number;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
  // 确认成功即生成的版本化推荐包（status: generated=已生成；旧后端无此字段时缺省，不冒充成功）。
  package?: RecommendationPackageRef | null;
}
export type RecommendationMetrics = {
  ok?: boolean; job_id?: number; confirmed_recommendations: number;
  assessed_candidates: number; rate: number | null;
}
export type WorkflowArtifactDetail = {
  artifact_id: string; workflow_id?: string; step_id?: number | null;
  artifact_type: string; title: string; mime_type: string; content: string;
  content_size: number; content_truncated: boolean; metadata: Record<string, unknown>;
  validation_status: string; created_at?: string; downloadable: boolean;
  download_kind: 'file' | 'content' | 'none'; file_name: string; download_url: string;
}
export type WorkflowArtifactPayload = { ok?: boolean; artifact: WorkflowArtifactDetail }

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
// S4-5（N4）渠道效能学习：渠道×岗位原型连续 0 召回（非渠道故障）≥2 轮时复盘附降权建议。
// 本期仅建议不执行：配额调整待顾问确认，落痕 strategy_v2.channel_downweights 与决策树 rebalance 步。
export type ChannelDownweight = {
  channel: string; archetype_id?: string; streak?: number; rounds?: number; recall_total?: number;
  reason?: string; recommendation?: string;
}
// S4-5（N5）评估校准暴露：高分 0 且评估数 ≥5 时复盘附"评估尺度复核"条目（prompt 固定"是尺严还是人不行"），
// items 为 ≤3 个被否人选（fit_score<75，按分高者先取）的评分证据链摘要：
// 遮罩名/当前公司职位/fit_score/关键扣分证据（criteria not_met 准则 + gaps，硬伤在前）。
export type EvaluationDeduction = {
  group?: string; criterion?: string; status?: string; critical?: boolean; reason?: string; evidence?: string[]
}
export type EvaluationReviewItem = {
  job_candidate_id?: number; assessment_id?: number; candidate?: string; company?: string; title?: string;
  fit_score?: number; fit_level?: string; recommendation?: string; deductions?: EvaluationDeduction[]
}
export type EvaluationReview = {
  kind?: string; prompt?: string; assessed_total?: number; high_score_total?: number;
  items?: EvaluationReviewItem[]; note?: string;
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
  channel_downweights?: ChannelDownweight[];
  evaluation_review?: EvaluationReview | null;
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

// S5-2 Mapping 任务卡：Core 返回动态 dict（openapi 只描述为 object），按 mapping_task.py
// mapping_v1 schema 与实际 payload 收窄声明。七态状态机（合法迁移由后端校验，409 透中文 detail）。
export type MappingCandidateStatus = 'pending' | 'confirmed' | 'contacted' | 'replied' | 'intaken' | 'parked' | 'rejected'
export type MappingTeamEvidence = { type?: string; ref?: string; as_of?: string }
export type MappingTargetTeam = {
  company: string; team?: string; location?: string; tier?: string;
  evidence?: MappingTeamEvidence[]; confidence?: string; notes?: string[];
}
export type MappingIcebreaker = { hooks: string[]; angle: string; generated_at?: string; source_ref?: string }
export type MappingIntakeReceipt = {
  job_candidate_id: number; candidate_id?: number; person_id?: number; intaken_at?: string; relation_existed?: boolean;
}
export type MappingCandidate = {
  name: string; current_role?: string; team_ref?: number; source_urls?: string[];
  confidence?: string; reason?: string; status: MappingCandidateStatus; consultant_note?: string;
  icebreaker?: MappingIcebreaker; intake?: MappingIntakeReceipt;
}
export type MappingFailure = { source?: string; url?: string; reason?: string; note?: string }
export type MappingTaskStats = {
  teams?: number; candidates?: number; confirmed?: number; intaken?: number; clues?: number;
  banned_filtered?: number; rejected_no_source?: number; pages_fetched?: number;
  failures_count?: number; failures?: MappingFailure[]; sources?: Record<string, number>;
}
export type MappingTaskDoc = {
  schema_version?: string; trigger?: string; job_id?: number; strategy_ref?: string;
  client?: string; job_title?: string; generated_at?: string;
  target_teams?: MappingTargetTeam[]; candidates?: MappingCandidate[]; stats?: MappingTaskStats;
}
export type MappingTaskPayload = {
  ok?: boolean; artifact_id: string; job_id?: number; workflow_id?: string;
  title?: string; content?: string; created_at?: string; mapping_task: MappingTaskDoc;
}
export type MappingTaskCreateResult = {
  ok?: boolean; job_id: number; workflow_id?: string; artifact_id: string; mapping_task?: MappingTaskDoc;
}
// PATCH 候选响应：confirmed 迁移自动生成破冰素材；素材不合格不阻断状态变更，原因进 icebreaker_errors。
export type MappingCandidatePatchResult = {
  ok?: boolean; artifact_id?: string; index?: number; candidate: MappingCandidate;
  status: string; status_label?: string; stats?: MappingTaskStats;
  icebreaker_generated?: boolean; icebreaker_errors?: string[];
}
export type MappingIcebreakerResult = {
  ok?: boolean; artifact_id?: string; index?: number; icebreaker: MappingIcebreaker; candidate: MappingCandidate;
}
export type MappingIntakeResult = {
  ok?: boolean; artifact_id?: string; index?: number; status: string;
  already_intaken?: boolean; relation_existed?: boolean;
  job_candidate_id: number; candidate_id?: number; person_id?: number; intaken_at?: string;
  stats?: MappingTaskStats;
}

// S6-1b 判人评估：Core 返回动态 dict（openapi 只描述为 object），按 assessment_v1 schema 收窄声明。
// PATCH advisor-action 为 S6-1b 后端新上，generated/api.d.ts 尚无，待主控 regenerate 后补锚。
export type AssessmentAdvisorAction = 'pending' | 'accepted' | 'modified' | 'rejected'
export type AssessmentEvidence = { type?: string; ref?: string }
export type AssessmentSegment = {
  company?: string; title?: string; period?: string; tier?: string; tier_source?: string;
  team?: string; report_line?: string; note?: string;
}
export type AssessmentMove = {
  from?: string; to?: string; direction?: string; platform?: string;
  title_direction?: string; responsibility_direction?: string; reason?: string;
}
export type AssessmentTrajectory = {
  verdict?: string; evidence?: AssessmentEvidence[]; confidence?: string;
  segments?: AssessmentSegment[]; promotion_pace?: string; tech_evolution?: string;
}
export type AssessmentMoveHistory = {
  verdict?: string; evidence?: AssessmentEvidence[]; confidence?: string;
  moves?: AssessmentMove[]; current_move?: string;
}
// S6-2 水平分位：band/参照系由后端确定性落位；sample_sufficient=false → UI 标「推测」。
export type AssessmentPercentileReference = {
  n?: number | null; direction?: string; years_window?: number | null;
  median?: number | null; q25?: number | null; q75?: number | null;
  min?: number | null; max?: number | null;
  sample_sufficient?: boolean; min_n?: number; note?: string;
}
export type AssessmentPercentile = {
  verdict?: string; band?: string | null; basis?: string; score?: number | null;
  percentile_rank?: number | null; reference?: AssessmentPercentileReference;
  evidence?: AssessmentEvidence[]; confidence?: string;
}
// S6-2 动机与时机：signals 为确定性产出（简历工况/公开信息带来源 URL）。
export type AssessmentMotivationSignal = {
  kind?: string; kind_label?: string; source?: string; summary?: string;
  url?: string; as_of?: string; evidence_line?: string;
}
export type AssessmentMotivation = {
  verdict?: string; signals?: AssessmentMotivationSignal[];
  evidence?: AssessmentEvidence[]; confidence?: string;
}
// S6-3 需要核实的问题：severity 三档 + 每条证据；items 空 → 「未见需核实的问题」。
export type AssessmentRiskItem = {
  kind?: string; risk?: string; severity?: string; evidence?: AssessmentEvidence[];
}
export type AssessmentRisks = {
  verdict?: string; items?: AssessmentRiskItem[];
  evidence?: AssessmentEvidence[]; confidence?: string;
}
export type CandidateAssessmentDoc = {
  schema_version?: string; candidate_id?: number; job_id?: number; candidate_name_masked?: string;
  job_title?: string; client?: string; as_of?: string; updated_at?: string;
  assessor_version?: string; model?: string;
  dimensions?: {
    trajectory?: AssessmentTrajectory | null;
    move_history?: AssessmentMoveHistory | null;
    percentile?: AssessmentPercentile | null;
    motivation?: AssessmentMotivation | null;
    risks?: AssessmentRisks | null;
  };
  consultant_summary?: string; advisor_action?: AssessmentAdvisorAction; advisor_note?: string;
}
export type CandidateAssessmentPayload = {
  ok?: boolean; candidate_id?: number; job_id?: number; artifact_id?: string;
  title?: string; content?: string; created_at?: string; updated_at?: string;
  advisor_action?: AssessmentAdvisorAction; advisor_note?: string;
  assessment?: CandidateAssessmentDoc;
}

// S6-4 评估校准度量（顾问点头率）：维度×客户聚合；数据不足的分组三个率为 null（前端如实呈现「数据不足」）。
export type CalibrationMetricsGroup = {
  client: string; dimension: string; dimension_label: string;
  total: number; accepted: number; modified: number; rejected: number;
  acceptance_rate: number | null; modified_rate: number | null; rejected_rate: number | null;
}
export type CalibrationMetricsPayload = {
  ok: boolean; generated_at?: string; min_n?: number;
  totals?: { assessments: number; pending: number; accepted: number; modified: number; rejected: number };
  groups?: CalibrationMetricsGroup[];
  labels?: { title?: string; acceptance_rate?: string; modified_rate?: string; rejected_rate?: string; insufficient?: string };
}

// S8 岗位画像（这个岗位实际在干什么）：Core 返回动态 dict，按 job_profile_insights.py 实际 payload 收窄声明。
// status: ready=可展示 / insufficient=履历还太少 / not_generated=尚未学习；disputed 为顾问"不对"标记留痕区。
export type JobProfileExample = { candidate: string; evidence: string }
export type JobProfileItem = { key: string; label: string; count: number; ratio: number; examples: JobProfileExample[] }
export type JobProfileDisputedItem = {
  item_type: string; key: string; label: string; count: number; note?: string; disputed_at?: string
}
export type JobProfileInsightsPayload = {
  ok?: boolean; job_id?: number; status?: 'ready' | 'insufficient' | 'not_generated';
  source_count?: number; min_source_count?: number; as_of?: string; version?: number;
  duties?: JobProfileItem[]; tools?: JobProfileItem[]; deliverables?: JobProfileItem[]; customers?: JobProfileItem[];
  disputed?: JobProfileDisputedItem[]; stats?: Record<string, unknown>;
}
export type JobProfileFeedbackResult = {
  ok?: boolean; status?: string; already_disputed?: boolean; item_type?: string; item_key?: string
}

// 版本化推荐包闭环：顾问确认推荐 → 推荐包（候选摘要/人岗证据/风险/待核验问题）→ 客户反馈按包版本留痕。
// Core 返回动态 dict（openapi 只描述为 object），按 asa_core/service.py 推荐包方法实际 payload 收窄声明。
export type RecommendationPackageRef = {
  package_id: string; version: number; status: string; created_at?: string; recommendation_id?: number;
}
export type RecommendationPackageSummary = RecommendationPackageRef & { feedback_count?: number }
export type RecommendationPackageFeedback = {
  id: number; feedback_type: string; feedback_type_label: string; content: string;
  feedback_time?: string; recorded_by?: string; created_at?: string;
}
export type RecommendationPackageEvidence = {
  // status: ready=有当前有效判人评估 / no_current_assessment=证据缺失（如实呈现，不伪造）。
  status?: string; note?: string; assessment_id?: number; fit_score?: number; fit_level?: string;
  recommendation?: string; confidence?: number; evidence_coverage?: number;
  criteria?: Record<string, unknown>; strengths?: string[]; gaps?: string[]; assessed_at?: string;
}
export type RecommendationPackageSummaryBlock = {
  name?: string; current_company?: string; current_title?: string; city?: string;
  education?: string; experience?: string; stage?: string;
  job?: { id?: number; title?: string; client?: string };
  recommendation?: { id?: number; reason?: string; confirmed_by?: string; confirmed_at?: string };
}
export type RecommendationPackageDetailPayload = RecommendationPackageRef & {
  ok?: boolean; candidate_id?: number; person_id?: number; job_id?: number;
  summary?: RecommendationPackageSummaryBlock;
  evidence?: RecommendationPackageEvidence;
  risks?: string[];
  verification_questions?: string[];
  feedback?: RecommendationPackageFeedback[];
}
export type PackageFeedbackResult = WriteAck & {
  package_id?: string; package_version?: number; candidate_id?: number;
  feedback_id?: number; event_id?: number; already_recorded?: boolean;
  feedback?: RecommendationPackageFeedback;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}

// 生命周期一等事件（面试/Offer/入职）：Core 返回动态 dict，按 asa_core/service.py record_lifecycle_event
// 实际 payload 收窄声明；新路由 generated/api.d.ts 尚无，主控 regenerate 后可补锚。
export type LifecycleEventResult = WriteAck & {
  candidate_id?: number; event_id?: number; followup_task_id?: number | null; already_recorded?: boolean;
  event?: { id: number; event_type: string; event_type_label: string; event_status: string; event_time?: string; summary?: string };
  followup?: { id: number; task_type: string; due_at?: string; status?: string } | null;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}

// 策略复盘客户端缓存：30 秒 TTL，避免切换 tab 时重复请求。
// 导出 clearStrategyReviewCache 供测试重置。
const STRATEGY_REVIEW_CACHE_TTL = 30_000
const _strategyReviewCache = new Map<string, { payload: StrategyReviewPayload | null; ts: number }>()
export const clearStrategyReviewCache = () => _strategyReviewCache.clear()

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
  workbench: () => json<Workbench>(`/api/v1/workbench?limit=${WORKBENCH_LIMIT}`),
  agentSessions: (limit = 30, q = '') => json<unknown>(`/api/v1/copilot/sessions?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ''}`).then(parseAgentSessionList),
  agentSession: (sessionId: string, limit = 100) => json<unknown>(`/api/v1/copilot/sessions/${encodeURIComponent(sessionId)}?limit=${limit}`).then(parseAgentSession),
  health: () => json<{ ok?: boolean; status?: string }>('/api/v1/health'),
  updateAgentSession: (sessionId: string, patch: { title?: string; archived?: boolean; clear_focus?: boolean }) =>
    write<unknown>(`/api/v1/copilot/sessions/${encodeURIComponent(sessionId)}`, patch, 'PATCH').then(parseAgentSessionUpdate),
  archiveAllAgentSessions: () =>
    write<{ ok: boolean; archived_count: number; session_ids: string[] }>('/api/v1/copilot/sessions/archive-all', {}),
  analysisRun: (runId: string) => json<{ ok: boolean; result: AnalysisResult; duration_ms: number; template_id?: string | null }>(`/api/v1/analytics/runs/${encodeURIComponent(runId)}`),
  createAnalysis: (catalogId: string, question = '', scope: Record<string, unknown> = {}) =>
    write<{ ok: boolean; result: AnalysisResult; duration_ms: number }>('/api/v1/analytics/runs', { catalog_id: catalogId, question, scope }),
  refreshAnalysis: (runId: string) =>
    write<{ ok: boolean; result: AnalysisResult; duration_ms: number }>(`/api/v1/analytics/runs/${encodeURIComponent(runId)}/refresh`, {}),
  exportAnalysis: (runId: string) =>
    write<{ ok: boolean; artifact: { artifact_id: string; download_url: string; title: string } }>(`/api/v1/analytics/runs/${encodeURIComponent(runId)}/export`, {}),
  analyticsCatalog: () => json<{ ok: boolean; version: string; items: AnalysisCatalogItem[] }>('/api/v1/analytics/catalog'),
  analyticsTemplates: () => json<{ ok: boolean; items: AnalysisTemplate[] }>('/api/v1/analytics/templates'),
  createAnalyticsTemplate: (input: AnalysisTemplateInput) =>
    write<{ ok: boolean; template_id: string }>('/api/v1/analytics/templates', input),
  updateAnalyticsTemplate: (templateId: string, input: AnalysisTemplateInput) =>
    write<{ ok: boolean; template_id: string }>(`/api/v1/analytics/templates/${encodeURIComponent(templateId)}`, input, 'PATCH'),
  runAnalyticsTemplate: (templateId: string) =>
    write<{ ok: boolean; result: AnalysisResult; duration_ms: number }>(`/api/v1/analytics/templates/${encodeURIComponent(templateId)}/run`, {}),
  analyticsTemplateRuns: (templateId: string) =>
    json<{ ok: boolean; items: AnalysisTemplateRun[] }>(`/api/v1/analytics/templates/${encodeURIComponent(templateId)}/runs`),
  analyticsTemplateTrend: (templateId: string) =>
    json<AnalysisTrend>(`/api/v1/analytics/templates/${encodeURIComponent(templateId)}/trend`),
  deleteAnalyticsTemplate: (templateId: string) =>
    json<{ ok: boolean; template_id: string; status: string }>(`/api/v1/analytics/templates/${encodeURIComponent(templateId)}`, { method: 'DELETE' }),
  setInboxState: (itemKey: string, state: WorkbenchItem['inbox_state'], sourceRevision: string) =>
    write<{ ok: boolean; item_key: string; state: string }>(`/api/v1/inbox/${encodeURIComponent(itemKey)}/state`, { state, source_revision: sourceRevision }, 'PATCH'),
  agentProposals: (status = 'pending', limit = 20) =>
    json<{ ok: boolean; status: string; proposals: AgentProposal[] }>(`/api/v1/agent/proposals?status=${encodeURIComponent(status)}&limit=${limit}`),
  agentActionMetrics: (days = 7) => json<{ ok: boolean; window_days: number; metrics: AgentActionMetrics }>(`/api/v1/agent/metrics?days=${days}`),
  modelAudit: (limit = 50, operation = '', status = '') =>
    json<ModelAuditResponse>(`/api/v1/agent/model-audit?limit=${limit}${operation ? `&operation=${encodeURIComponent(operation)}` : ''}${status ? `&status=${encodeURIComponent(status)}` : ''}`),
  floatingState: () => json<FloatingStatePayload>('/api/asa/floating/state'),
  floatingAction: (action: string, payload: Record<string, unknown> = {}) =>
    write<FloatingActionResult>('/api/asa/floating/action', { action, ...payload }),
  agentCandidateAssess: (jobCandidateId: number, force = true, trigger = 'agent_page_context') =>
    write<AgentCandidateAssessResult>('/api/agent/candidate-assess', { job_candidate_id: jobCandidateId, force, trigger }),
  agentRun: (runId: string) =>
    json<AgentRunResult>(`/api/agent/run?run_id=${encodeURIComponent(runId)}`),
  generateAgentProposals: (jobCandidateIds: number[] = [], limit = 12) =>
    write<{ ok: boolean; proposals: AgentProposal[]; skipped: Array<Record<string, unknown>> }>('/api/v1/agent/proposals/generate', { job_candidate_ids: jobCandidateIds, limit }),
  preflightAgentProposal: (proposalId: string) =>
    write<AgentProposalPreflight>(`/api/v1/agent/proposals/${encodeURIComponent(proposalId)}/preflight`, {}),
  decideAgentProposal: (proposalId: string, confirmationToken: string, decision: 'approve' | 'reject', note = '') =>
    write<WriteAck>(`/api/v1/agent/proposals/${encodeURIComponent(proposalId)}/decision`, { confirmation_token: confirmationToken, decision, note }),
  jobs: (q = '', limit = 200, offset = 0) =>
    json<{items: Job[]; total: number; limit?: number; offset?: number}>(
      `/api/v1/jobs?limit=${limit}&offset=${offset}&q=${encodeURIComponent(q)}`,
    ),
  allJobs: async (q = '') => {
    const limit = 200
    const first = await api.jobs(q, limit, 0)
    const items = [...first.items]
    while (items.length < first.total) {
      const page = await api.jobs(q, limit, items.length)
      if (!page.items.length) break
      items.push(...page.items)
    }
    return { items, total: first.total }
  },
  job: (id: number) => json<{job: JobDetail}>(`/api/v1/jobs/${id}`),
  candidates: (q = '', jobId?: number, limit = 200, offset = 0) =>
    json<{items: Candidate[]; total: number; limit?: number; offset?: number}>(
      `/api/v1/candidates?limit=${limit}&offset=${offset}&q=${encodeURIComponent(q)}${jobId ? `&job_id=${jobId}` : ''}`,
    ),
  allCandidates: async (q = '', jobId?: number) => {
    const limit = 200
    const first = await api.candidates(q, jobId, limit, 0)
    const items = [...first.items]
    while (items.length < first.total) {
      const page = await api.candidates(q, jobId, limit, items.length)
      if (!page.items.length) break
      items.push(...page.items)
    }
    return { items, total: first.total }
  },
  candidate: (id: number) => json<{candidate: CandidateDetail}>(`/api/v1/candidates/${id}`),
  workflow: async (id: string): Promise<Workflow> => parseWorkflow(await json<unknown>(`/api/v1/workflows/${encodeURIComponent(id)}`)),
  workflowArtifact: (artifactId: string) =>
    json<WorkflowArtifactPayload>(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`),
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
  // 30 秒客户端缓存：同一 workflow_id 切换 tab 时不重复请求。
  strategyReview: async (id: string): Promise<StrategyReviewPayload | null> => {
    const cached = _strategyReviewCache.get(id)
    if (cached && Date.now() - cached.ts < STRATEGY_REVIEW_CACHE_TTL) return cached.payload
    try {
      const payload = await json<StrategyReviewPayload>(`/api/v1/workflows/${encodeURIComponent(id)}/strategy-review`)
      _strategyReviewCache.set(id, { payload, ts: Date.now() })
      return payload
    } catch (error) {
      if ((error as { status?: number }).status === 404) {
        _strategyReviewCache.set(id, { payload: null, ts: Date.now() })
        return null
      }
      throw error
    }
  },
  // 按需重算（终局工作流补生成）：幂等走 write 封装；非终局 Core 返回 409，错误带 status 由调用方呈现。
  // 成功后清除该 workflow_id 的缓存，以便 strategyReview 重新拉取。
  rebuildStrategyReview: async (id: string) => {
    const result = await write<StrategyReviewRebuildResult>(`/api/v1/workflows/${encodeURIComponent(id)}/strategy-review/rebuild`, {})
    _strategyReviewCache.delete(id)
    return result
  },
  // S4-3c 逐项采纳/拒绝回写：upsert 可重复覆盖，与 revise 各自幂等（不同 Idempotency-Key）。
  // 404=工作流/复盘不存在，409=diff_id 未知或状态非法；错误带 status 由调用方决定降级策略。
  patchStrategyReviewDiffs: (id: string, decisions: StrategyReviewDiffDecision[]) =>
    write<StrategyReviewDiffPatchResult>(`/api/v1/workflows/${encodeURIComponent(id)}/strategy-review/diffs`, { decisions }, 'PATCH'),
  // S5-2 Mapping 任务卡：读走 GET；创建/PATCH/重新生成/入库全走 write 幂等封装
  // （Idempotency-Key + request_id，重放返回首次响应）。404=artifact/候选不存在；
  // 409=业务冲突（非法迁移/终态/禁挖/无来源，中文 detail 直接透出）。
  mappingTask: (jobId: number, artifactId: string) =>
    json<MappingTaskPayload>(`/api/v1/jobs/${jobId}/mapping-tasks/${encodeURIComponent(artifactId)}`),
  createMappingTask: (jobId: number, trigger: string) =>
    write<MappingTaskCreateResult>(`/api/v1/jobs/${jobId}/mapping-tasks`, { trigger }),
  patchMappingCandidate: (artifactId: string, index: number, patch: { status?: MappingCandidateStatus; consultant_note?: string }) =>
    write<MappingCandidatePatchResult>(`/api/v1/mapping-tasks/${encodeURIComponent(artifactId)}/candidates/${index}`, { ...patch }, 'PATCH'),
  regenerateMappingIcebreaker: (artifactId: string, index: number) =>
    write<MappingIcebreakerResult>(`/api/v1/mapping-tasks/${encodeURIComponent(artifactId)}/candidates/${index}/icebreaker`, {}),
  intakeMappingCandidate: (artifactId: string, index: number) =>
    write<MappingIntakeResult>(`/api/v1/mapping-tasks/${encodeURIComponent(artifactId)}/candidates/${index}/intake`, {}),
  // S6-1b 判人评估：无评估时 Core 返回 404，此处收敛为 null（其余错误照常抛出，携带 status）。
  // 生成/重生成与顾问动作写回全走 write 幂等封装；409=无简历语料/模型不可用/非法 action（中文 detail 透出）。
  candidateAssessment: async (candidateId: number, jobId: number): Promise<CandidateAssessmentPayload | null> => {
    try {
      return await json<CandidateAssessmentPayload>(`/api/v1/candidates/${candidateId}/assessments?job_id=${jobId}`)
    } catch (error) {
      if ((error as { status?: number }).status === 404) return null
      throw error
    }
  },
  generateCandidateAssessment: (candidateId: number, jobId: number, force = false) =>
    write<CandidateAssessmentPayload>(`/api/v1/candidates/${candidateId}/assessments?job_id=${jobId}${force ? '&force=true' : ''}`, {}),
  patchAssessmentAdvisorAction: (candidateId: number, jobId: number, action: AssessmentAdvisorAction, note?: string) =>
    write<CandidateAssessmentPayload>(`/api/v1/candidates/${candidateId}/assessments/${jobId}/advisor-action`, { action, ...(note ? { note } : {}) }, 'PATCH'),
  // S6-4 评估校准度量（只读）：数据不足的分组率为 null，由展示层如实呈现。
  assessmentCalibrationMetrics: () =>
    json<CalibrationMetricsPayload>('/api/v1/assessments/calibration/metrics'),
  // S8 岗位画像（这个岗位实际在干什么）：GET 永远 200（岗位存在时），status 决定展示/空态；
  // "不对"回写走 write 幂等封装（Idempotency-Key + request_id，重放返回首次响应），409=非法条目类型。
  // 两个 S8 路由已入 ContractAnchor（generated/api.d.ts 已 regenerate）。
  jobProfileInsights: (jobId: number) =>
    json<JobProfileInsightsPayload>(`/api/v1/jobs/${jobId}/profile-insights`),
  disputeJobProfileItem: (jobId: number, item: { item_type: string; item_key: string; item_label?: string; note?: string }) =>
    write<JobProfileFeedbackResult>(`/api/v1/jobs/${jobId}/profile-insights/feedback`, { ...item }),
  workflowAction: (id: string, action: string, payload: Record<string, unknown> = {}) => write(`/api/v1/workflows/${id}/${action}`, payload),
  retryStep: (id: number) => json<WriteAck>(`/api/agent/steps/${id}/retry`, { method: 'POST', body: '{}' }),
  approval: (id: string, decision: string) => write(`/api/v1/approvals/${id}/decision`, { decision }),
  preflight: (candidate_id: number, action: string) => {
    const body: CandidateActionRequest = { request_id: requestId(), candidate_id, action }
    return json<PreflightResult>('/api/v1/candidate-actions/preflight', { method: 'POST', body: JSON.stringify(body) })
  },
  commit: (candidate_id: number, action: string, preflight_token: string, note = '', reason?: string) => {
    const body: Omit<CandidateActionBody, 'request_id' | 'reason'> & { reason?: string } = { candidate_id, action, preflight_token, note, ...(reason ? { reason } : {}) }
    return write<CandidateActionResult>('/api/v1/candidate-actions/commit', body)
  },
  consultantRecommendationPreflight: (candidate_id: number) => {
    const body = { request_id: requestId(), candidate_id }
    return json<ConsultantRecommendationPreflight>('/api/v1/consultant-recommendations/preflight', { method: 'POST', body: JSON.stringify(body) })
  },
  consultantRecommendationCommit: (candidate_id: number, reason: string, preflight_token: string) =>
    write<ConsultantRecommendationResult>('/api/v1/consultant-recommendations/commit', { candidate_id, reason, preflight_token }),
  recommendationMetrics: (job_id: number) =>
    json<RecommendationMetrics>(`/api/v1/jobs/${job_id}/recommendation-metrics`),
  // 寻访策略按项编辑（本期新上，generated/api.d.ts 尚无此路由，主控 regenerate 后可补锚）。
  // 走 write 幂等封装（Idempotency-Key + request_id，重放返回首次响应）；
  // 404=工作流/策略不存在，409=外部寻访已开始/目标项缺失/质量校验不过（中文 detail 透出）。
  applyStrategyEdits: (workflowId: string, edits: StrategyItemEdit[], note = '') =>
    write<StrategyItemEditResult>(`/api/v1/workflows/${encodeURIComponent(workflowId)}/strategy/edits`, { edits, note }),
  // 版本化推荐包：列表随候选人详情返回（recommendation_packages 字段），详情/反馈按需拉取与写入。
  // 详情 404=推荐包不存在；反馈走 write 幂等封装，404=推荐包不存在，409=反馈类型非法/内容为空。
  recommendationPackages: (candidateId: number) =>
    json<{ ok?: boolean; candidate_id?: number; items: RecommendationPackageSummary[] }>(`/api/v1/candidates/${candidateId}/recommendation-packages`),
  recommendationPackage: (packageId: string) =>
    json<RecommendationPackageDetailPayload>(`/api/v1/recommendation-packages/${encodeURIComponent(packageId)}`),
  recordPackageFeedback: (packageId: string, input: { feedback_type: string; content: string; feedback_time?: string }) =>
    write<PackageFeedbackResult>(`/api/v1/recommendation-packages/${encodeURIComponent(packageId)}/feedback`, { ...input }),
  // 生命周期一等事件（面试/Offer/入职）：写候选人时间线并自动生成跟进待办（不自动对外沟通）。
  // 走 write 幂等封装；404=候选人不存在，409=事件类型/状态/时间格式非法（中文 detail 透出）。
  recordLifecycleEvent: (candidateId: number, input: { event_type: string; occurred_at?: string; event_status?: string; notes?: string }) =>
    write<LifecycleEventResult>(`/api/v1/candidates/${candidateId}/lifecycle-events`, { ...input }),
  // 岗位自动周报：生成走 write 幂等封装（同周更新同一 artifact，version 自增）；
  // 列表只读，尚无周报时 latest=null/items=[]；完整正文走 workflowArtifact 查看器。
  generateJobWeeklyReport: (jobId: number) =>
    write<JobWeeklyReportCreateResult>(`/api/v1/jobs/${jobId}/weekly-report`, {}),
  jobWeeklyReports: (jobId: number) =>
    json<JobWeeklyReportListPayload>(`/api/v1/jobs/${jobId}/weekly-reports`),
  // 二期知识飞轮：knowledge_proposal 知识增补提案。列表/详情只读；生成走 write 幂等封装
  // （同内容不重复提案）；确认走 preflight（300s 令牌+内容签名）→ decision 两段，
  // 409=令牌无效/过期/内容漂移/非法决策/拒绝缺原因（中文 detail 透出）。
  knowledgeProposals: (status = 'pending', limit = 50, proposalType = '') =>
    json<KnowledgeProposalListPayload>(`/api/v1/knowledge-proposals?status=${encodeURIComponent(status)}&limit=${limit}${proposalType ? `&proposal_type=${encodeURIComponent(proposalType)}` : ''}`),
  knowledgeProposal: (proposalId: string) =>
    json<KnowledgeProposalDetailPayload>(`/api/v1/knowledge-proposals/${encodeURIComponent(proposalId)}`),
  generateKnowledgeProposals: (limit = 50) =>
    write<KnowledgeProposalGenerateResult>('/api/v1/knowledge-proposals/generate', { limit }),
  preflightKnowledgeProposal: (proposalId: string) =>
    write<KnowledgeProposalPreflight>(`/api/v1/knowledge-proposals/${encodeURIComponent(proposalId)}/preflight`, {}),
  decideKnowledgeProposal: (proposalId: string, confirmationToken: string, decision: KnowledgeProposalDecisionKind, note = '') =>
    write<KnowledgeProposalDecisionResult>(`/api/v1/knowledge-proposals/${encodeURIComponent(proposalId)}/decision`, { confirmation_token: confirmationToken, decision, note }),
  // 二期知识飞轮：核心公司校准（company_calibration）。队列/进度/详情只读；提交走 write
  // 幂等封装（Idempotency-Key + request_id；同内容重提不 bump version，重放返回首次响应）。
  // 404=公司不在图谱，409=校准状态非法（中文 detail 透出）。
  companyCalibrations: (q = '', status = '', limit = 50) =>
    json<CompanyCalibrationQueuePayload>(`/api/v1/company-calibrations?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ''}${status ? `&status=${encodeURIComponent(status)}` : ''}`),
  companyCalibrationProgress: () =>
    json<CompanyCalibrationProgress>('/api/v1/company-calibrations/progress'),
  companyCalibrationDetail: (companyKey: string) =>
    json<CompanyCalibrationDetailPayload>(`/api/v1/company-calibrations/${encodeURIComponent(companyKey)}`),
  submitCompanyCalibration: (input: CompanyCalibrationSubmitInput) =>
    write<CompanyCalibrationResult>('/api/v1/company-calibrations', { ...input }),
}

// 寻访策略按项编辑：op 枚举与参数见后端 strategy_editor.SUPPORTED_OPS；[key: string] unknown
// 承载各 op 的参数位（group/terms/targets/tier/name/accepted_levels/constraints 等）。
export type StrategyItemEdit = { op: string } & Record<string, unknown>
export type StrategyItemEditResult = WriteAck & {
  workflow_id?: string; revision?: number; edit_count?: number;
  applied?: Array<{ op?: string; summary?: string }>;
  artifact_id?: string; approval_refreshed?: boolean;
  strategy_hash?: string; query_plan_hash?: string; strategy_ready?: boolean;
  strategy_v2?: Record<string, unknown>;
  receipt?: { idempotent_replay?: boolean; request_id?: string };
}

// 岗位自动周报：Core 返回动态 dict，按 asa_core/job_weekly_report.py 实际 payload 收窄声明。
export type JobWeeklyReportSummary = {
  total?: number | null; active?: number | null; recommended?: number | null;
  confirmed_this_week?: number | null; comparison?: string;
  risk_count?: number; suggestion_count?: number;
}
export type JobWeeklyReportBrief = {
  artifact_id: string; title: string; version: number;
  week_start: string; week_end: string; generated_at?: string; created_at?: string;
  validation_status?: string; summary?: JobWeeklyReportSummary;
}
export type JobWeeklyReportListPayload = {
  ok?: boolean; job_id?: number; latest: JobWeeklyReportBrief | null; items: JobWeeklyReportBrief[];
}
export type JobWeeklyReportCreateResult = WriteAck & {
  job_id?: number; artifact_id?: string; version?: number;
  week_start?: string; week_end?: string;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}

const requestId = () => `web_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`
const write = <T = WriteAck>(url: string, payload: Record<string, unknown>, method: 'POST' | 'PATCH' = 'POST'): Promise<T> => {
  const request_id = requestId()
  return json<T>(url, { method, headers: { 'Idempotency-Key': `${request_id}_${url}` }, body: JSON.stringify({ ...payload, request_id }) })
}

// 二期知识飞轮（knowledge_proposal）：Core 返回动态 dict（openapi 只描述为 object），
// 按 asa_core/knowledge_proposals.py 实际 payload 收窄声明。本期为后端新上路由，
// generated/api.d.ts 尚无，待主控 regenerate 后补锚。
export type KnowledgeProposalType = 'company_graph_entry' | 'skill_alias' | 'level_mapping' | 'negative_rule'
export type KnowledgeProposalStatus = 'pending' | 'accepted' | 'rejected' | 'superseded'
export type KnowledgeProposalDecisionKind = 'accept' | 'reject'
export type KnowledgeProposalBrief = {
  proposal_id: string; proposal_type: KnowledgeProposalType; proposal_type_label: string;
  title: string; status: KnowledgeProposalStatus; status_label: string;
  created_at?: string; decided_at?: string | null; decided_by?: string | null;
}
export type KnowledgeProposalEvidence = {
  source_type?: string; source_ids?: number[]; summary?: string;
  samples?: Array<Record<string, unknown>>;
}
export type KnowledgeProposalContent = {
  scope_type?: string; scope?: string; rule?: string; rationale?: string;
  name?: string; track?: string; business?: string; categories?: string[];
  trigger?: string; trigger_code?: string; occurrences?: number;
}
export type KnowledgeProposalDetailPayload = KnowledgeProposalBrief & {
  ok?: boolean; content: KnowledgeProposalContent; evidence: KnowledgeProposalEvidence[];
  applied_to?: string | null; decision_note?: string | null; updated_at?: string;
}
export type KnowledgeProposalListPayload = {
  ok?: boolean; status: string; items: KnowledgeProposalBrief[];
  counts?: Partial<Record<KnowledgeProposalStatus, number>>;
  type_labels?: Record<string, string>;
}
// 低于阈值/不可结构化的聚类只留候选不生成提案，如实呈现。
export type KnowledgeProposalCandidate = { kind?: string; key?: string; count?: number; needed?: number; reason?: string }
export type KnowledgeProposalGenerateResult = WriteAck & {
  created?: KnowledgeProposalBrief[]; existing?: KnowledgeProposalBrief[];
  candidates?: KnowledgeProposalCandidate[];
  thresholds?: { stop_reason_min_cluster?: number; feedback_min_cluster?: number; company_min_cluster?: number };
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}
export type KnowledgeProposalPreflight = {
  ok?: boolean; confirmation_token: string; expires_in?: number; signature?: string;
  proposal?: KnowledgeProposalDetailPayload; impact?: string;
}
export type KnowledgeProposalDecisionResult = WriteAck & {
  proposal_id?: string; decision?: KnowledgeProposalDecisionKind;
  status?: KnowledgeProposalStatus; status_label?: string; applied_to?: string;
  proposal?: KnowledgeProposalDetailPayload;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}

// 二期知识飞轮：核心公司校准（company_calibration）。图谱 JSON 保持原始名单，校准是 DB 覆盖层；
// Core 返回动态 dict（openapi 只描述为 object），按 asa_core/company_calibration.py 实际 payload
// 收窄声明。本期为后端新上路由，generated/api.d.ts 尚无，待主控 regenerate 后补锚。
export type CompanyCalibrationStatus = 'calibrated' | 'rejected' | 'needs_review'
// 队列条目额外有 pending（未校准：无校准记录）。
export type CompanyCalibrationQueueStatus = CompanyCalibrationStatus | 'pending'
export type CompanyCalibrationRecord = {
  calibration_id: string; company_key: string; company_name: string;
  track: string; product_lines: string[]; skill_tags: string[]; level_system: string;
  no_poach: boolean; non_compete: boolean; note: string;
  status: CompanyCalibrationStatus; status_label: string;
  calibrated_by?: string; calibrated_at?: string; version: number; updated_at?: string;
}
export type CompanyCalibrationQueueItem = {
  company_key: string; company_name: string; track: string; business: string; categories: string[];
  status: CompanyCalibrationQueueStatus; status_label: string;
  calibration: CompanyCalibrationRecord | null;
}
export type CompanyCalibrationQueuePayload = {
  ok?: boolean; status: string; query?: string;
  items: CompanyCalibrationQueueItem[]; total: number;
  status_labels?: Record<string, string>;
}
export type CompanyCalibrationProgress = {
  ok?: boolean; target: number; calibrated: number; needs_review: number; rejected: number;
  pending: number; total: number; ratio: number | null;
}
export type CompanyCalibrationDetailPayload = CompanyCalibrationQueueItem & { ok?: boolean }
export type CompanyCalibrationSubmitInput = {
  company_name: string; status: CompanyCalibrationStatus;
  track?: string; product_lines?: string[]; skill_tags?: string[]; level_system?: string;
  no_poach?: boolean; non_compete?: boolean; note?: string; calibrated_by?: string;
}
export type CompanyCalibrationResult = WriteAck & {
  changed?: boolean; company_key?: string; company_name?: string;
  status?: CompanyCalibrationStatus; status_label?: string; version?: number;
  calibration?: CompanyCalibrationRecord; progress?: CompanyCalibrationProgress;
  receipt?: { idempotent_replay?: boolean; request_id?: string; audit_event_id?: number };
}
