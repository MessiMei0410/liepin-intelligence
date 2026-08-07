import type { Workflow } from '../api'

const TYPE_LABELS: Record<string, string> = {
  search_strategy: '多渠道寻访策略',
  sourcing_ticket: '多渠道寻访执行任务',
  jd_calibration: 'JD 校准结果',
  candidate_assessment: '简历评估',
  matching_report: '人岗匹配报告',
  recommendation_report: '推荐报告',
  resume_document: '结构化简历',
  salary_report: '薪酬报告',
  mapping_task: 'Mapping 直挖任务',
  strategy_review: '策略复盘',
  external_action_receipt: '外部动作回执',
  job_publish_draft: '岗位发布草稿',
  job_publish_prepare_readback: '岗位发布预检读回',
  outreach_draft_batch: '触达草稿',
  radar_scan: '人才雷达扫描',
  radar_weekly_report: '人才雷达周报',
}

const STATUS_LABELS: Record<string, string> = {
  passed: '通过校验',
  pending: '待校验',
  pending_execution: '待执行',
  warning: '有风险',
  blocked: '已阻塞',
  failed: '校验失败',
}

export const artifactTypeLabel = (value: string): string => TYPE_LABELS[value] || '业务产物'
export const artifactStatusLabel = (value: string): string => STATUS_LABELS[value] || '状态待确认'

export const artifactAbsenceMessage = (workflow: Workflow): string => {
  const serverMessage = String(workflow.artifact_summary?.message || '').trim()
  if (serverMessage) return serverMessage
  const status = workflow.workflow.status
  if (status === 'planned') return '计划尚未执行，开始后这里会显示业务产物或结果去向。'
  if (['queued', 'running', 'waiting_approval', 'waiting_external'].includes(status)) {
    return '工作流仍在执行，产物会在对应步骤完成并校验后出现。'
  }
  if (status === 'cancelled') return '工作流已取消；取消前没有生成可查看产物。'
  if (['failed', 'blocked'].includes(status)) return '工作流未形成可查看产物，请先处理失败或阻塞步骤。'
  return '本工作流完成的是核验或状态更新，结果已回写业务记录，不另生成文件产物。'
}
