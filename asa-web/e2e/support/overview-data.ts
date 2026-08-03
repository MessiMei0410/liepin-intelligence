import type { AnalysisResult, Candidate, Dashboard, Job, Workbench } from '../../src/api'

export const overviewDashboard: Dashboard = {
  ok: true,
  counts: {
    active_jobs: 12,
    candidates: 48,
    pending_candidates: 17,
    pending_approvals: 2,
    pending_proposals: 0,
    executed_proposals: 0,
    failed_proposals: 0,
  },
  workflows: [
    {
      workflow_id: 'workflow_fixture_01',
      status: 'waiting_approval',
      title: '示例客户甲 | 运动控制负责人 | 第2轮寻访',
      current_stage: 'search_strategy',
    },
    {
      workflow_id: 'workflow_fixture_02',
      status: 'running',
      title: '示例客户乙 | 精密机械专家 | 第1轮寻访',
      current_stage: 'multi_channel_sourcing',
    },
    {
      workflow_id: 'workflow_fixture_03',
      status: 'completed',
      business_outcome: 'completed_target_met',
      title: '示例客户丙 | 技术市场经理 | 第3轮寻访',
      current_stage: 'candidate_batch_assessment',
    },
    {
      workflow_id: 'workflow_fixture_04',
      status: 'completed',
      business_outcome: 'completed_needs_review',
      title: '示例客户丁 | 软件架构师 | 第2轮寻访',
      current_stage: 'candidate_batch_assessment',
    },
    {
      workflow_id: 'workflow_fixture_05',
      status: 'blocked',
      title: '示例客户戊 | 失效分析工程师 | 第1轮寻访',
      current_stage: 'multi_channel_sourcing',
    },
    {
      workflow_id: 'workflow_fixture_06',
      status: 'cancelled',
      title: '示例客户己 | 电气工程师 | 第1轮寻访',
      current_stage: 'search_strategy',
    },
    {
      workflow_id: 'workflow_fixture_07',
      status: 'superseded',
      title: '示例客户庚 | 产品专家 | 第1轮寻访',
      current_stage: 'job_calibration',
    },
    {
      workflow_id: 'workflow_fixture_08',
      status: 'paused',
      title: '示例客户辛 | 控制算法专家 | 第2轮寻访',
      current_stage: 'search_strategy',
    },
  ],
}

export const overviewJobs: Job[] = [
  { id: 9001, client: '示例客户甲', title: '运动控制负责人', priority: 'P0 紧急', candidate_count: 18, active_candidate_count: 12 },
  { id: 9002, client: '示例客户乙', title: '精密机械专家', priority: 'P0 紧急', candidate_count: 14, active_candidate_count: 9 },
  { id: 9003, client: '示例客户丙', title: '技术市场经理', priority: 'P1 优先', candidate_count: 11, active_candidate_count: 7 },
  { id: 9004, client: '示例客户丁', title: '软件架构师', priority: 'P1 优先', candidate_count: 8, active_candidate_count: 5 },
  { id: 9005, client: '示例客户戊', title: '失效分析工程师', priority: 'P2 常规', candidate_count: 5, active_candidate_count: 3 },
  { id: 9006, client: '示例客户己', title: '电气工程师', priority: 'P2 常规', candidate_count: 3, active_candidate_count: 1 },
]

export const overviewCandidates: Candidate[] = [
  { id: 9101, person_id: 9201, name: '候选人甲', current_company: '精密设备公司', current_title: '运动控制工程师', job_id: 9001, job: '运动控制负责人', client: '示例客户甲', source_type: 'xsaas', clean_stage: 'X1 X-SaaS新增/待复核', updated_at: '2026-07-30 10:08:00' },
  { id: 9102, person_id: 9202, name: '候选人乙', current_company: '半导体装备公司', current_title: '机械设计工程师', job_id: 9002, job: '精密机械专家', client: '示例客户乙', source_type: 'liepin', clean_stage: 'S1 新增寻访/待复核', updated_at: '2026-07-30 09:42:00' },
  { id: 9103, person_id: 9203, name: '候选人丙', current_company: '功率器件公司', current_title: '技术市场经理', job_id: 9003, job: '技术市场经理', client: '示例客户丙', source_type: 'liepin', clean_stage: '已触达', updated_at: '2026-07-30 09:15:00' },
  { id: 9104, person_id: 9204, name: '候选人丁', current_company: '工业软件公司', current_title: '软件架构师', job_id: 9004, job: '软件架构师', client: '示例客户丁', source_type: 'xsaas', clean_stage: 'X2 已复核/待人工联系', updated_at: '2026-07-29 18:30:00' },
  { id: 9105, person_id: 9205, name: '候选人戊', current_company: '封装测试公司', current_title: '失效分析工程师', job_id: 9005, job: '失效分析工程师', client: '示例客户戊', source_type: 'liepin', clean_stage: 'S3 已回复', updated_at: '2026-07-29 17:50:00' },
  { id: 9106, person_id: 9206, name: '候选人己', current_company: '自动化公司', current_title: '电气工程师', job_id: 9006, job: '电气工程师', client: '示例客户己', source_type: 'xsaas', clean_stage: 'X3 已申请加微信/待通过', updated_at: '2026-07-29 16:20:00' },
  { id: 9107, person_id: 9207, name: '候选人庚', current_company: '芯片设计公司', current_title: '产品专家', job_id: 9003, job: '技术市场经理', client: '示例客户丙', source_type: 'liepin', clean_stage: '已触达', updated_at: '2026-07-29 15:10:00' },
  { id: 9108, person_id: 9208, name: '候选人辛', current_company: '机器人公司', current_title: '控制算法专家', job_id: 9001, job: '运动控制负责人', client: '示例客户甲', source_type: 'liepin', clean_stage: 'S1 新增寻访/待复核', updated_at: '2026-07-29 14:05:00' },
]

export const overviewWorkbench: Workbench = {
  ok: true, version: 'fixture-v1', summary: { pending: 4, running: 1, delivered: 2, total: 7 },
  items: [
    { item_key: 'approval:1', source_revision: '1', kind: 'approval', lane: 'pending', priority_score: 20000, title: '示例客户甲｜运动控制负责人｜第2轮寻访', subtitle: '执行多渠道寻访', status_label: 'R3 待审批', reason: '外部动作需由顾问单次确认', source_label: '审批', updated_at: '2026-08-03 09:30:00', inbox_state: 'unread', primary_action: { type: 'open_workflow', id: 'workflow_fixture_01', label: '查看并审批' } },
    { item_key: 'candidate:9105', source_revision: '1', kind: 'candidate_action', lane: 'pending', priority_score: 15000, title: '候选人戊', subtitle: '示例客户戊 / 失效分析工程师', status_label: '已回复', reason: '候选人回复，希望进一步了解团队与汇报线', source_label: '人选推进', updated_at: '2026-08-03 09:20:00', inbox_state: 'unread', primary_action: { type: 'open_candidate', id: '9105', label: '处理候选人回复' } },
    { item_key: 'candidate:9102', source_revision: '1', kind: 'candidate_action', lane: 'pending', priority_score: 11000, title: '候选人乙', subtitle: '示例客户乙 / 精密机械专家', status_label: '待复核', reason: '新增简历，等待人工复核推进方向', source_label: '人选推进', updated_at: '2026-08-03 09:10:00', inbox_state: 'unread', primary_action: { type: 'open_candidate', id: '9102', label: '人工复核' } },
    { item_key: 'candidate:9104', source_revision: '1', kind: 'candidate_action', lane: 'pending', priority_score: 9000, title: '候选人丁', subtitle: '示例客户丁 / 软件架构师', status_label: '待联系', reason: '复核通过，等待顾问人工联系', source_label: '人选推进', updated_at: '2026-08-03 08:55:00', inbox_state: 'unread', primary_action: { type: 'open_candidate', id: '9104', label: '查看人选' } },
    { item_key: 'analysis:1', source_revision: '1', kind: 'analysis', lane: 'delivered', priority_score: 0, title: '17 项人选推进需要关注，2 个 P0 岗位优先', subtitle: '经营概览', status_label: '已交付', reason: '', source_label: '分析', updated_at: '2026-08-03 08:30:00', inbox_state: 'unread', primary_action: { type: 'open_analysis', id: 'analysis_fixture', label: '查看分析' } },
  ],
}

export const overviewAnalysis: AnalysisResult = {
  schema_version: 'analysis_result_v1', run_id: 'analysis_fixture', catalog_id: 'operations_overview', catalog_version: '2026-08-03', status: 'completed',
  question: '今天最需要先处理什么？', scope: { days: 7 }, data_as_of: '2026-08-03T09:35:00+08:00',
  headline: '17 项人选推进需要关注，2 个 P0 岗位优先',
  metrics: [
    { id: 'active_jobs', label: '开放岗位', value: 12, unit: 'count', definition_id: 'asa.active_jobs', definition_version: '2026-08-03' },
    { id: 'active_candidates', label: '有效人选', value: 48, unit: 'count', definition_id: 'asa.active_candidates', definition_version: '2026-08-03' },
    { id: 'pending_candidates', label: '待处理人选', value: 17, unit: 'count', definition_id: 'asa.pending_candidates', definition_version: '2026-08-03' },
    { id: 'p0_jobs', label: 'P0 岗位', value: 2, unit: 'count', definition_id: 'asa.p0_jobs', definition_version: '2026-08-03' },
  ],
  sections: [{ type: 'table', title: '岗位关注顺序', columns: ['client', 'title', 'active_candidates', 'candidates'], rows: overviewJobs.slice(0, 5).map(job => ({ client: job.client, title: job.title, active_candidates: job.active_candidate_count, candidates: job.candidate_count })) }],
  references: overviewJobs.slice(0, 4).map(job => ({ type: 'job', id: job.id, label: `${job.client} / ${job.title}`, href: `#job=${job.id}` })),
  caveats: ['停止推进与已淘汰历史已从待处理口径排除。'], truncated: false, suggested_actions: [], supersedes_run_id: null,
}
