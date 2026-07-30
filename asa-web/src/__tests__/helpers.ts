import type { CandidateDetail, Workflow } from '../api'

// fetch 最小 Response 替身：api.ts 只读取 ok/status/statusText/json()。
export const mockResponse = (body: unknown, ok = true, status = 200) =>
  ({ ok, status, statusText: ok ? 'OK' : 'Error', json: () => Promise.resolve(body) }) as unknown as Response

export const candidateDetail: CandidateDetail = {
  id: 1,
  person_id: 101,
  name: '张三',
  current_company: '示例科技',
  current_title: '前端工程师',
  city: '上海',
  client: 'ACME',
  job: '前端工程师',
  clean_stage: 'S1 待复核',
  is_stopped: false,
  resume: { summary: '', full_text: '', work_text: '', project_text: '', education_text: '', raw: {} },
  source_links: [],
  events: [],
  job_relations: [],
  sourcing_attributions: [],
}

export const plannedWorkflow: Workflow = {
  ok: true,
  plan_ref: { workflow_id: 'wf-1', version: 1, plan_hash: 'plan-hash-1' },
  goal: { title: '寻访前端工程师', objective: '为 ACME 寻访前端工程师', status: 'planned', progress: 0 },
  workflow: { workflow_id: 'wf-1', status: 'planned' },
  progress: { completed: 0, total: 2, ratio: 0 },
  steps: [
    { id: 1, sequence: 1, business_label: '生成寻访策略', risk_level: '低', status: 'pending' },
    { id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'pending' },
  ],
  approvals: [],
  artifacts: [],
  events: [],
}
