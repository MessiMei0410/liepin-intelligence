import type { Workflow } from '../api'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import { arrayValue, recordValue } from '../shared/records'
import { artifactAbsenceMessage } from './artifactPresentation'

// R9 本轮交付速览：工作流详情顶部的紧凑业务摘要，只从当前 workflow/step/artifact payload 推导，
// 不发起请求、不拼接虚构数据。终态一律给业务解释并复用 statusMapping 中文文案，
// 绝不渲染 status/business_outcome 英文原形；数据缺失时用“等待渠道回执 / 暂无候选结果”兜底。

const TERMINAL_KINDS = ['target_met', 'needs_review', 'pool_insufficient', 'technical_failed'] as const
const terminalKinds = new Set<string>(TERMINAL_KINDS)

const stepOutput = (step?: Workflow['steps'][number]): Record<string, unknown> => {
  if (!step) return {}
  let data = recordValue(step.output)
  if (!Object.keys(data).length && step.output_json) {
    try {
      data = recordValue(JSON.parse(step.output_json))
    } catch {
      // 存量畸形 output_json 不做渲染依据，回落为空对象。
    }
  }
  return data
}

const riskLabel = (level?: string): string => {
  const value = String(level || '').trim().toUpperCase()
  if (value === 'R3' || value === '高') return '高风险'
  if (value === 'R2' || value === '中') return '中风险'
  if (value === 'R1') return '中低风险'
  if (value === 'R0' || value === '低') return '低风险'
  return value ? `风险（${level}）` : '风险'
}

const businessOutcomeOf = (workflow: Workflow): string | null | undefined =>
  workflow.business_outcome ?? workflow.workflow.business_outcome ?? workflow.goal.business_outcome

export const deliveryBottleneck = (workflow: Workflow): string => {
  const mapped = mapWorkflowStatus({ status: workflow.workflow.status, business_outcome: businessOutcomeOf(workflow), steps: workflow.steps })
  if (terminalKinds.has(mapped.kind)) return mapped.label
  const status = workflow.workflow.status
  const pendingApproval = workflow.approvals.find(item => item.status === 'pending')
  if (status === 'waiting_approval') return pendingApproval ? `等待审批：${pendingApproval.title}` : mapped.label
  const failedStep = workflow.steps.find(step => step.status === 'failed')
  if (failedStep) return `执行失败：${failedStep.business_label}`
  const blockedStep = workflow.steps.find(step => step.status === 'blocked')
  if (blockedStep) return `流程阻塞：${blockedStep.business_label}`
  const waitingExternal = workflow.steps.find(step => step.status === 'waiting_external')
  if (waitingExternal) return `等待渠道返回：${waitingExternal.business_label}`
  const runningStep = workflow.steps.find(step => step.status === 'running')
  if (runningStep) return `执行中：${runningStep.business_label}`
  const queuedStep = workflow.steps.find(step => step.status === 'queued')
  if (queuedStep) return `排队中：${queuedStep.business_label}`
  if (status === 'paused') return '已暂停'
  if (status === 'planned') return '计划就绪，等待确认'
  if (status === 'cancelled') return '本轮已取消'
  if (status === 'completed') {
    const completed = workflow.progress?.completed ?? workflow.steps.filter(step => ['completed', 'skipped'].includes(step.status)).length
    const total = workflow.progress?.total ?? workflow.steps.length
    return `本轮已完成 ${completed}/${total} 步`
  }
  return mapped.label
}

export const deliveryRisk = (workflow: Workflow): string => {
  const failedStep = workflow.steps.find(step => step.status === 'failed')
  if (failedStep) return `${riskLabel(failedStep.risk_level)}：${failedStep.business_label} 执行失败`
  const blockedStep = workflow.steps.find(step => step.status === 'blocked')
  if (blockedStep) return `${riskLabel(blockedStep.risk_level)}：${blockedStep.business_label} 阻塞中`
  const pendingApproval = workflow.approvals.find(item => item.status === 'pending')
  if (pendingApproval) return `${riskLabel(pendingApproval.risk_level)}：${pendingApproval.title} 待审批`
  const activeStep = workflow.steps.find(step => ['running', 'waiting_external', 'waiting_approval', 'queued'].includes(step.status) && String(step.risk_level || '').trim())
  if (activeStep) return `${riskLabel(activeStep.risk_level)}：${activeStep.business_label}`
  return '当前无风险标记'
}

export const deliveryNextAction = (workflow: Workflow): string => {
  const mapped = mapWorkflowStatus({ status: workflow.workflow.status, business_outcome: businessOutcomeOf(workflow), steps: workflow.steps })
  if (mapped.kind === 'needs_review' || mapped.kind === 'pool_insufficient') return '复核现有人选或在 Agent 中调整策略'
  if (mapped.kind === 'target_met') return '本轮目标达成，无待处理动作'
  if (mapped.kind === 'technical_failed') {
    const failedStep = workflow.steps.find(step => step.status === 'failed')
    return failedStep ? `重试失败步骤：${failedStep.business_label}` : '修复失败步骤后重试'
  }
  const status = workflow.workflow.status
  const pendingApproval = workflow.approvals.find(item => item.status === 'pending')
  if (pendingApproval) return `处理待审批：${pendingApproval.title}`
  const failedStep = workflow.steps.find(step => step.status === 'failed')
  if (failedStep) return `重试失败步骤：${failedStep.business_label}`
  if (status === 'blocked') {
    const blockedStep = workflow.steps.find(step => step.status === 'blocked')
    return blockedStep ? `处理阻塞步骤：${blockedStep.business_label}` : '处理流程阻塞后继续'
  }
  if (status === 'waiting_external') return '等待渠道返回寻访结果'
  if (status === 'waiting_approval') return '等待审批通过后继续执行'
  const activeStep = workflow.steps.find(step => ['running', 'queued'].includes(step.status))
  if (activeStep) return `等待当前步骤完成：${activeStep.business_label}`
  if (status === 'paused') return '继续寻访'
  if (status === 'planned') return '确认计划并准备'
  if (status === 'cancelled') return '本轮已结束，可归档或重新发起'
  if (status === 'completed') return '本轮已完成，无待处理动作'
  return mapped.showNextActions ? '复核现有人选或在 Agent 中调整策略' : '等待系统处理'
}

export const deliveryCandidateOverview = (workflow: Workflow): string => {
  const assessmentStep = workflow.steps.find(step => step.capability_id === 'candidate_batch_assessment')
  const assessmentOutput = stepOutput(assessmentStep)
  const queue = recordValue(assessmentOutput.assessment_queue)
  const summary = String(assessmentOutput.summary || '').trim()
  const roundAssessed = arrayValue(queue.completed_items).length || Number(queue.started || 0)
  const assessedTotal = Number(queue.completed || 0)
  if (assessmentStep?.status === 'completed' && (Object.keys(queue).length > 0 || summary)) {
    return summary || `本轮评估 ${roundAssessed} 位 · 岗位累计已评估 ${assessedTotal} 人`
  }
  const sourcingStep = workflow.steps.find(step => step.capability_id === 'multi_channel_sourcing')
  const output = stepOutput(sourcingStep)
  const funnel = recordValue(recordValue(output.diagnosis).funnel)
  const funnelTotal = Number(funnel.total || 0)
  const pendingReview = Number(funnel.pending_review || 0)
  const external = recordValue(output.external_result)
  const runs = arrayValue(external.channel_runs)
  const recallTotal = runs.reduce<number>((sum, item) => sum + Number(recordValue(recordValue(item).result).candidates || 0), 0)
  const applied = recordValue(recordValue(external.intake).applied)
  const inserted = Number(applied.inserted || 0)
  const skipped = Number(applied.skipped_existing || 0)
  const parts: string[] = []
  if (funnelTotal > 0) parts.push(`人才漏斗 ${funnelTotal} 人，待复核 ${pendingReview} 人`)
  if (recallTotal > 0) parts.push(`渠道合计召回 ${recallTotal} 条候选`)
  if (inserted > 0 || skipped > 0) parts.push(`本轮新增入库 ${inserted} 人，跳过已有 ${skipped} 人`)
  if (parts.length) return parts.join('；')
  const finished = ['completed', 'failed', 'blocked'].includes(workflow.workflow.status)
  return finished ? '暂无候选结果' : '等待渠道回执'
}

// R10 预计产出（一期缺口）：预期值只从 workflow detail 现有字段 additive 读取——
// 策略预期召回 = search_strategy 步 output.strategy_v2.step5_expectation.expected_recall_per_tier 求和；
// 目标人数 = multi_channel_sourcing 步 output.external_request.target_count，回落审批 preflight.target_count。
// 计划/执行中显示预期；终态显示实际 vs 预期（实际召回=渠道 channel_runs 合计，实际入库=intake.applied.inserted）；
// 两处都没有数据时如实「未设定预期产出」，不编造。
const expectedRecallOf = (workflow: Workflow): number => {
  const strategyStep = workflow.steps.find(step => step.capability_id === 'search_strategy')
  const expectation = recordValue(recordValue(stepOutput(strategyStep).strategy_v2).step5_expectation)
  const perTier = recordValue(expectation.expected_recall_per_tier)
  return Object.values(perTier).reduce<number>((sum, value) => sum + (Number(value) || 0), 0)
}

const targetCountOf = (workflow: Workflow): number => {
  const sourcingStep = workflow.steps.find(step => step.capability_id === 'multi_channel_sourcing')
  const fromStep = Number(recordValue(stepOutput(sourcingStep).external_request).target_count || 0)
  if (fromStep > 0) return fromStep
  const approval = workflow.approvals.find(item => Number(recordValue(item.preflight).target_count || 0) > 0)
  return approval ? Number(recordValue(approval.preflight).target_count || 0) : 0
}

export const deliveryExpectedOutput = (workflow: Workflow): string => {
  const expectedRecall = expectedRecallOf(workflow)
  const targetCount = targetCountOf(workflow)
  if (expectedRecall <= 0 && targetCount <= 0) return '未设定预期产出'
  const mapped = mapWorkflowStatus({ status: workflow.workflow.status, business_outcome: businessOutcomeOf(workflow), steps: workflow.steps })
  const terminal = terminalKinds.has(mapped.kind) || ['completed', 'failed', 'blocked'].includes(workflow.workflow.status)
  if (!terminal) {
    const parts: string[] = []
    if (expectedRecall > 0) parts.push(`预计召回 ${expectedRecall} 条候选`)
    if (targetCount > 0) parts.push(`目标 ${targetCount} 人`)
    return parts.join(' · ')
  }
  const sourcingStep = workflow.steps.find(step => step.capability_id === 'multi_channel_sourcing')
  const external = recordValue(stepOutput(sourcingStep).external_result)
  const runs = arrayValue(external.channel_runs)
  const actualRecall = runs.reduce<number>((sum, item) => sum + Number(recordValue(recordValue(item).result).candidates || 0), 0)
  const actualInserted = Number(recordValue(recordValue(external.intake).applied).inserted || 0)
  const parts: string[] = []
  if (expectedRecall > 0) parts.push(`实际召回 ${actualRecall} 条（预期 ${expectedRecall}）`)
  if (targetCount > 0) parts.push(`实际入库 ${actualInserted} 人（目标 ${targetCount}）`)
  return parts.join(' · ')
}

export const deliveryArtifactReason = (workflow: Workflow): string => {
  const artifacts = workflow.artifacts
  if (artifacts.length > 0) {
    const titles = artifacts.map(item => item.title).filter(Boolean)
    const shown = titles.slice(0, 2).join('、')
    return `已生成 ${artifacts.length} 个产物${shown ? `：${shown}${artifacts.length > 2 ? ' 等' : ''}` : ''}`
  }
  return `未生成产物：${artifactAbsenceMessage(workflow)}`
}

export function BusinessDeliverySummary({ workflow }: { workflow: Workflow }) {
  const mapped = mapWorkflowStatus({ status: workflow.workflow.status, business_outcome: businessOutcomeOf(workflow), steps: workflow.steps })
  const rows = [
    { label: '当前瓶颈', value: deliveryBottleneck(workflow) },
    { label: '风险', value: deliveryRisk(workflow) },
    { label: '下一步', value: deliveryNextAction(workflow) },
    { label: '预计产出', value: deliveryExpectedOutput(workflow) },
    { label: '候选结果', value: deliveryCandidateOverview(workflow) },
    { label: '产物', value: deliveryArtifactReason(workflow) },
  ]
  return (
    <section className="workflow-summary workflow-delivery" aria-label="本轮交付速览">
      <div className="workflow-delivery-head" style={{ display: 'grid', gridTemplateColumns: 'auto minmax(0, 1fr)', gap: '8px', alignItems: 'baseline' }}>
        <b>本轮交付速览</b>
        <span>{mapped.label}</span>
      </div>
      {rows.map(row => (
        <div className="workflow-delivery-row" key={row.label} style={{ display: 'grid', gridTemplateColumns: '64px minmax(0, 1fr)', gap: '10px', alignItems: 'baseline' }}>
          <span>{row.label}</span>
          <b>{row.value}</b>
        </div>
      ))}
    </section>
  )
}
