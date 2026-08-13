import { expect, skipIfNoBackend, test } from '../support/fixtures'

skipIfNoBackend()

const candidate = {
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
  resume: { summary: '8年研发经验，当前关注核心产品交付。', full_text: '', work_text: '', project_text: '', education_text: '', raw: {} },
  source_links: [],
  events: [],
  job_relations: [],
  sourcing_attributions: [{
    id: 1, channel: '猎聘', source_query: '前端工程师', source_round: 'T1', source_purpose: '核心同层人才池',
    learning_score: 80, signal_count: 3, review_pass_count: 1, contacted_count: 1, recommended_count: 1,
    stopped_count: 0, client_positive_count: 1, client_rejected_count: 0,
  }],
  sourcing_recalls: [
    {
      recall_id: 'recall-1', run_id: 'run-lineage-7', workflow_id: 'wf-7', strategy_hash: 'hash-7',
      strategy_artifact_id: 'artifact-7', strategy_revision: 7, query_plan_hash: 'plan-7',
      query_cell_id: 'cell-core-peer-2', query_family_ids: ['keyword_group:power'],
      query_provenance: [{ kind: 'keyword_group', tier: 'T1', group: '核心同层', targets: '服务器电源' }],
      channel: 'liepin', source_query: '前端工程师', created_at: '2026-08-14 03:00:00',
    },
    {
      recall_id: 'recall-2', run_id: 'run-lineage-8', workflow_id: 'wf-8', strategy_hash: 'hash-8',
      strategy_artifact_id: 'artifact-8', strategy_revision: 8, query_plan_hash: 'plan-8',
      query_cell_id: 'cell-company-3', query_family_ids: ['company_keyword:Example'],
      query_provenance: [{ kind: 'company_keyword', tier: 'T2', company: '示例科技', path: '相邻行业' }],
      channel: 'liepin', source_query: '  前端工程师  ', created_at: '2026-08-14 04:00:00',
    },
    {
      recall_id: 'legacy-recall', run_id: 'legacy-run', query_cell_id: 'legacy-cell', channel: 'xsaas',
      source_query: '结构工程师', created_at: '2026-08-11 18:36:08',
    },
  ],
  report_artifacts: [],
  recommendation_packages: [],
}

test('候选人详情保留长期来源效果与每一次精确寻访执行', async ({ page }) => {
  await page.route('**/api/v1/candidates/1', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ candidate }),
  }))
  await page.goto('/asa-app#candidate=1')

  const panel = page.locator('.candidate-panel')
  await expect(panel).toBeVisible()
  const trace = panel.locator('.sourcing-trace')
  await expect(trace).toContainText('寻访来源 · 3 次执行')
  await expect(trace.locator('.sourcing-trace-row')).toHaveCount(2)
  await expect(trace.locator('.sourcing-recall')).toHaveCount(3)
  await expect(trace).toContainText('猎聘 · T1')
  await expect(trace).toContainText('执行 run-lineage-7')
  await expect(trace).toContainText('策略 revision 8 · 单元 cell-company-3')
  await expect(trace).toContainText('历史记录未保存策略版本 · 单元 legacy-cell')

  await page.setViewportSize({ width: 390, height: 700 })
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.body.clientWidth)).toBeTruthy()
  await expect(trace).toBeVisible()
})
