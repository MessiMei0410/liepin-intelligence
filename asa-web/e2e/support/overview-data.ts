import type { Candidate, Dashboard, Job } from '../../src/api'

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
