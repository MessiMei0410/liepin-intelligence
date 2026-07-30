import { z } from 'zod'

// R8 渠道寻访漏斗边界模型：GET /api/v1/workflows/{id}/sourcing-funnel。
// 数据由执行器在寻访落库时写入 agent_sourcing_funnel 表（capability_runtime._persist_sourcing_funnel），
// Core 聚合为 channels（渠道级合计）+ runs（run×channel 明细，含 queries 每组查询）。
// 与 workflowModel 同一纪律：结构字段严格、标量叶子宽松、looseObject 透传未知键；
// parse 失败降级为宽松透传并 console.warn，channels/runs 归一化为空数组，渲染层不做防御判断。

const recordValue = z.record(z.string(), z.unknown())

// 详情抓取三态分布：complete+partial+failed ≤ extracted_count（守恒口径见 docs/sourcing-funnel-metrics.md）。
export const funnelDetailSchema = z.looseObject({
  complete: z.number(),
  partial: z.number(),
  failed: z.number(),
  complete_rate: z.number().nullish(),
})
export type FunnelDetail = z.infer<typeof funnelDetailSchema>

// 渠道级聚合：同一工作流下该渠道所有 run 的合计；zero_attribution 取最新一轮非空归因。
export const sourcingFunnelChannelSchema = z.looseObject({
  channel: z.string(),
  runs: z.number(),
  status: z.string().nullish(),
  recall_count: z.number(),
  extracted_count: z.number(),
  dedupe_count: z.number(),
  unique_count: z.number(),
  intake_duplicate_count: z.number(),
  intake_new_count: z.number(),
  assessed_count: z.number(),
  high_score_count: z.number(),
  detail: funnelDetailSchema,
  zero_attribution: z.string().nullish(),
})
export type SourcingFunnelChannel = z.infer<typeof sourcingFunnelChannelSchema>

// run 级明细：queries 为每组查询的 runner 原始记录（query/result_count/extracted_count/status/reason 等，随 runner 演进保持开放）。
export const sourcingFunnelRunSchema = z.looseObject({
  run_id: z.string().nullish(),
  channel: z.string().nullish(),
  status: z.string().nullish(),
  query_count: z.number(),
  queries: z.array(recordValue),
  recall_count: z.number(),
  extracted_count: z.number(),
  dedupe_count: z.number(),
  unique_count: z.number(),
  intake_duplicate_count: z.number(),
  intake_new_count: z.number(),
  assessed_count: z.number(),
  high_score_count: z.number(),
  detail: funnelDetailSchema,
  zero_attribution: z.string().nullish(),
  error: z.string().nullish(),
  created_at: z.string().nullish(),
  updated_at: z.string().nullish(),
})
export type SourcingFunnelRun = z.infer<typeof sourcingFunnelRunSchema>

export const coverageCertificateSchema = z.looseObject({
  schema_version: z.literal('coverage_certificate_v1'),
  certificate_id: z.string(),
  coverage_status: z.string(),
  query_cells: z.looseObject({
    approved: z.number(),
    executed: z.number(),
    exhausted: z.number(),
    platform_capped: z.number(),
    blocked: z.number(),
    failed: z.number(),
    pending: z.number().default(0),
  }),
  candidate_recall: z.looseObject({
    raw_occurrences: z.number(),
    unique_identities: z.number(),
    duplicate_occurrences: z.number(),
    below_threshold: z.number(),
    formally_intaked: z.number(),
  }),
  evidence_integrity: z.looseObject({
    passed: z.boolean(),
    expected_extracted_occurrences: z.number(),
    mapped_recall_occurrences: z.number(),
    unmapped_recall_occurrences: z.number(),
    mismatched_query_cells: z.number(),
  }).nullish(),
  dimension_execution: z.looseObject({
    retrieval_axes: z.array(z.string()),
    platform_filters_applied: z.array(z.string()),
    dimensions: z.record(z.string(), z.looseObject({
      approved_values: z.array(z.string()),
      retrieval_filter_applied: z.boolean(),
      evaluation_mode: z.string(),
    })),
  }).nullish(),
  detail_completeness: z.looseObject({
    complete: z.number(),
    partial: z.number(),
    failed: z.number(),
  }),
  assessment: z.looseObject({ completed_unique_candidates: z.number() }),
  claims: z.looseObject({
    all_candidates_covered: z.boolean(),
    defensible_claim: z.string(),
    coverage_unknown_reasons: z.array(z.string()),
  }),
})
export type CoverageCertificate = z.infer<typeof coverageCertificateSchema>

export const sourcingFunnelSchema = z.looseObject({
  ok: z.boolean(),
  workflow_id: z.string().optional(),
  channels: z.array(sourcingFunnelChannelSchema),
  runs: z.array(sourcingFunnelRunSchema),
  coverage_certificate: coverageCertificateSchema.nullish(),
})
export type SourcingFunnel = z.infer<typeof sourcingFunnelSchema>

const emptyDetail = (): FunnelDetail => ({ complete: 0, partial: 0, failed: 0, complete_rate: null })

// 边界 parse：漂移时宽松透传并告警；无论哪条路径都归一化 channels/runs/detail，渲染层拿到的永远是完整形状。
export const parseSourcingFunnel = (payload: unknown): SourcingFunnel => {
  const result = sourcingFunnelSchema.safeParse(payload)
  if (!result.success) {
    console.warn('[sourcingFunnel] 漏斗 payload 与 schema 漂移，降级为宽松透传。', result.error.issues)
  }
  const data = (result.success ? result.data : payload) as SourcingFunnel
  const record = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>
  const channels = (Array.isArray(record.channels) ? record.channels : []) as SourcingFunnelChannel[]
  const runs = (Array.isArray(record.runs) ? record.runs : []) as SourcingFunnelRun[]
  return {
    ...(data && typeof data === 'object' ? data : { ok: false }),
    channels: channels.map(channel => ({ ...channel, detail: channel.detail || emptyDetail() })),
    runs: runs.map(run => ({ ...run, detail: run.detail || emptyDetail(), queries: Array.isArray(run.queries) ? run.queries : [] })),
  }
}

// 某渠道的全部 run（通常一轮一个渠道一条 run；重跑/多轮时累加展示）。
export const channelRuns = (funnel: SourcingFunnel, channel: string): SourcingFunnelRun[] =>
  funnel.runs.filter(run => String(run.channel || '') === channel)

// 渠道级没有 query_count 字段（后端只在 run 级记录），展示口径为该渠道所有 run 的查询组数合计。
export const channelQueryCount = (funnel: SourcingFunnel, channel: string): number =>
  channelRuns(funnel, channel).reduce((sum, run) => sum + (Number(run.query_count) || 0), 0)

// 单组查询的展示文本：runner rounds 记录的主键是 query，兼容 keyword/text 历史写法。
export const queryText = (entry: Record<string, unknown>): string => {
  const value = entry.query ?? entry.keyword ?? entry.text
  return String(value ?? '').trim()
}
