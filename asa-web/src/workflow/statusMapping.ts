import type { Workflow } from '../api'
import { eventStatusLabel } from '../shared/format'

// R3 业务终态映射：把后端 (status, business_outcome) 二元组收敛为面板可直接渲染的
// 中文文案 + 语义色调 + 是否展示“下一步操作”。
// business_outcome 已核实的四枚枚举：completed_target_met / completed_needs_review /
// completed_pool_insufficient / failed_technical。出现未知新值时不进任何业务分支，
// 一律回落 status 原有逻辑，绝不渲染英文枚举原形。

export type WorkflowStatusTone = 'green' | 'amber' | 'red' | 'muted'

export type WorkflowStatusKind =
  | 'technical_failed'
  | 'target_met'
  | 'needs_review'
  | 'pool_insufficient'
  | 'flow_blocked'
  | 'default'

export type WorkflowStepLike = Pick<Workflow['steps'][number], 'status' | 'business_label'>

export type WorkflowStatusInput = {
  status: string
  business_outcome?: string | null
  steps?: WorkflowStepLike[]
}

export type WorkflowStatusMapping = {
  label: string
  tone: WorkflowStatusTone
  kind: WorkflowStatusKind
  showNextActions: boolean
}

// 与 main.tsx 原 status→中文映射一致（搬运，行为等价）；Overview 与 WorkflowPanel 共用。
export const workflowStatusLabel: Record<string, string> = {
  planned: '计划就绪', queued: '正在排队', running: '执行中', waiting_approval: '等待审批', waiting_external: '等待外部结果',
  blocked: '已阻塞', failed: '执行失败', completed: '已完成', cancelled: '已取消', paused: '已暂停', superseded: '已被修订版替代',
}

// 非业务终态分支的语义色调近似；面板对 kind==='default' 仍按原 stepTone(status) 取 CSS class。
const statusTone = (status: string): WorkflowStatusTone =>
  status === 'completed' ? 'green' : status === 'waiting_approval' ? 'amber' : ['failed', 'blocked'].includes(status) ? 'red' : 'muted'

// 技术失败附失败步骤名：取第一个 status==='failed' 的步骤业务名，无则省略。
const failedStepLabel = (steps: WorkflowStepLike[] = []) => steps.find(step => step.status === 'failed')?.business_label?.trim() || ''

export const mapWorkflowStatus = ({ status, business_outcome, steps }: WorkflowStatusInput): WorkflowStatusMapping => {
  // 技术失败：新发生的 failed 工作流 business_outcome 为 null，用 status 判；后端显式标记一并覆盖。
  if (status === 'failed' || business_outcome === 'failed_technical') {
    const step = failedStepLabel(steps)
    return { label: `技术失败${step ? `：${step}` : ''}`, tone: 'red', kind: 'technical_failed', showNextActions: false }
  }
  if (business_outcome === 'completed_target_met')
    return { label: '本轮完成，达成目标', tone: 'green', kind: 'target_met', showNextActions: false }
  if (business_outcome === 'completed_needs_review')
    return { label: '本轮完成，合格人数不足，有待复核人选', tone: 'amber', kind: 'needs_review', showNextActions: true }
  if (business_outcome === 'completed_pool_insufficient')
    return { label: '本轮完成，合格人数不足', tone: 'amber', kind: 'pool_insufficient', showNextActions: true }
  // 存量阻塞但无业务结论：技术/流程阻塞，与业务未达标明确区分。
  if (status === 'blocked' && !business_outcome)
    return { label: '流程阻塞，待处理', tone: 'muted', kind: 'flow_blocked', showNextActions: false }
  // 其余状态（含 business_outcome 未知新值）：沿用 status 原有文案与色调。
  return { label: workflowStatusLabel[status] || '状态待同步', tone: statusTone(status), kind: 'default', showNextActions: false }
}

// R8 零结果归因映射：以后端 classify_zero_result 的枚举为准（agent_sourcing_funnel.zero_attribution）。
// 0 召回/0 入库的渠道必须带中文解释，不得只显示 completed；未知新值回落“待排查”并保留原值便于排查。
// 六枚举中文文案以 docs/ASA_KIMI_HANDOFF_2026-07-22_ROUND2.md T2 映射表为准（逐字冻结，测试钉住）。
// S4-3c-1 新增 query_build_error/pool_saturated 两类，文案与后端 ZERO_RESULT_ATTRIBUTION_LABELS 逐字一致。
export const zeroAttributionLabels: Record<string, string> = {
  no_results: '该渠道真实无匹配结果',
  session_expired: '登录态失效，需重新登录该渠道',
  compliance_wall: '命中平台合规墙（需在浏览器里确认承诺函后重试）',
  loading_incomplete: '页面加载未完成或查询未生效',
  page_structure_changed: '页面结构变化，解析器需要适配',
  parse_failure: '平台有结果但解析抓取失败',
  query_build_error: '查询构造异常',
  pool_saturated: '本地人才库基本找遍了（重复率太高）',
  unknown: '原因待排查',
}

export const zeroAttributionLabel = (code?: string | null): string => {
  const key = String(code || '').trim()
  if (!key) return ''
  return zeroAttributionLabels[key] || `待排查（${key}）`
}

// 归因标签的语义色调：真实无结果是渠道健康的信息态，其余一律告警色。
export const zeroAttributionTone = (code?: string | null): 'muted' | 'warn' =>
  String(code || '').trim() === 'no_results' ? 'muted' : 'warn'

// ── 候选人侧英文枚举收口（P8）──────────────────────────────────────
// 寻访名单「意向」列与详情时间线「最近动态」不得渲染数据库枚举原形。
// 意向列的 intention 字段混装自由中文文本与 raw_status 枚举：枚举一律映射成中文，
// 无法识别的纯英文枚举值回落「状态待同步」，自由文本原样保留。

export const intentionStatusLabels: Record<string, string> = {
  search_shortlisted: '搜索入库 · 待复核',
  xsaas_search_shortlisted: 'X-SaaS 入库 · 待复核',
  candidate_intake: '已入库',
  screen_rejected: '初筛未通过',
  xsaas_review_stop: '复核未通过',
  rejected: '已淘汰',
  stopped: '已停止',
  closed: '已关闭',
}

export const intentionLabel = (value?: string | null): string => {
  const key = String(value || '').trim()
  if (!key) return '-'
  const known = intentionStatusLabels[key.toLowerCase()]
  if (known) return known
  // 漏网的纯英文枚举（小写+下划线形态）不直出；自由文本（多为中文沟通记录）原样保留。
  if (/^[a-z][a-z0-9_]*$/.test(key)) return '状态待同步'
  return key
}

// 时间线 event_status 增补枚举：shared/format.eventStatusLabel 的既有映射继续生效，
// 本表只补它漏掉的值；仍无法识别且含英文字母的回落「状态待同步」，绝不渲染英文原形。
export const candidateEventStatusLabels: Record<string, string> = {
  corrected: '已纠正',
  job_chat_verified: '猎聘触达已核验',
}

export const humanizeEventStatus = (value?: string | null): string => {
  const key = String(value || '').trim()
  if (!key) return ''
  const known = candidateEventStatusLabels[key]
  if (known) return known
  const label = eventStatusLabel(key)
  return /[A-Za-z]/.test(label) ? '状态待同步' : label
}
