import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openJob, openWorkflow } from '../support/nav'
import { overviewAnalysis, overviewCandidates, overviewDashboard, overviewJobs, overviewWorkbench } from '../support/overview-data'

skipIfNoBackend()

async function expectStableViewport(page: import('@playwright/test').Page) {
  const layout = await page.evaluate(() => ({
    viewport: window.innerWidth,
    pageWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
    visibleText: document.body.innerText.trim().length,
  }))
  expect(layout.pageWidth).toBeLessThanOrEqual(layout.viewport)
  expect(layout.bodyWidth).toBeLessThanOrEqual(layout.viewport)
  expect(layout.visibleText).toBeGreaterThan(100)
}

const fixedTemplate = {
  template_id: 'template_fixture', name: '每日经营变化', catalog_id: 'operations_overview',
  question: '今天的经营指标发生了什么变化？', scope: { days: 7 }, enabled: true,
  schedule_kind: 'daily', schedule_enabled: true, schedule_time: '09:00', schedule_weekday: 0,
  timezone: 'Asia/Shanghai', next_run_at: '2026-08-04T01:00:00+00:00',
  last_run_at: '2026-08-03T01:00:00+00:00', last_status: 'completed',
  last_run_id: 'analysis_fixture', last_result: overviewAnalysis, updated_at: '2026-08-03 09:00:00',
}

async function mockOverview(page: import('@playwright/test').Page) {
  await page.route('**/api/v1/dashboard', route => route.fulfill({ json: overviewDashboard }))
  await page.route('**/api/v1/jobs?**', route => route.fulfill({ json: { items: overviewJobs, total: overviewJobs.length } }))
  await page.route('**/api/v1/candidates?**', route => route.fulfill({ json: { items: overviewCandidates, total: overviewCandidates.length } }))
  await page.route('**/api/v1/workbench?**', route => route.fulfill({ json: overviewWorkbench }))
  await page.route('**/api/v1/analytics/catalog', route => route.fulfill({ json: {
    ok: true, version: '2026-08-03', items: [{ catalog_id: 'operations_overview', label: '经营概览', allowed_scope_fields: ['days'] }],
  } }))
}

// R6 截图回归：同一组用例跑两个 project——shots-desktop（1440×900）与
// shots-floating（390×700，验证窄屏任务抽屉布局）。基线 png 提交于 e2e/snapshots/。
// 等待条件保证异步数据全部落盘后再拍，避免基线抖动。

test('Agent 首页', async ({ page }) => {
  await page.route('**/api/v1/dashboard', route => route.fulfill({ json: overviewDashboard }))
  await page.route('**/api/v1/jobs?**', route => route.fulfill({ json: { items: overviewJobs, total: overviewJobs.length } }))
  await page.route('**/api/v1/candidates?**', route => route.fulfill({ json: { items: overviewCandidates, total: overviewCandidates.length } }))
  await page.route('**/api/v1/workbench?**', route => route.fulfill({ json: overviewWorkbench }))
  await page.route('**/api/v1/analytics/templates', route => route.fulfill({ json: { ok: true, items: [] } }))
  await page.route('**/api/v1/copilot/sessions?**', route => route.fulfill({ json: { ok: true, sessions: [] } }))
  await page.goto('/asa-app')
  await expect(page.locator('header.topbar')).toContainText('ASA Agent 在线')
  await expect(page.getByRole('heading', { name: '今天从哪里开始？' })).toBeVisible()
  await expect(page.getByRole('region', { name: '今日概况' })).toBeVisible()
  await expect(page.locator('.agent-home-band').first()).toBeVisible()
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('agent-home.png')
})

test('Agent 对话工作区', async ({ page }) => {
  await page.route('**/api/v1/dashboard', route => route.fulfill({ json: overviewDashboard }))
  await page.route('**/api/v1/jobs?**', route => route.fulfill({ json: { items: overviewJobs, total: overviewJobs.length } }))
  await page.route('**/api/v1/candidates?**', route => route.fulfill({ json: { items: overviewCandidates, total: overviewCandidates.length } }))
  await page.route('**/api/v1/workbench?**', route => route.fulfill({ json: overviewWorkbench }))
  await page.route('**/api/v1/analytics/templates', route => route.fulfill({ json: { ok: true, items: [] } }))
  await page.route('**/api/v1/copilot/sessions**', route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/agent-shot-task')) return route.fulfill({ json: {
      ok: true,
      session_id: 'agent-shot-task',
      business_focus: { context: { type: 'job', id: 154, client: '士兰微', job: '技术市场经理' } },
      messages: [
        { role: 'user', content: '复盘这个岗位当前寻访进度。' },
        { role: 'assistant', content: '## 核心结论\n\n本轮已完成初筛，建议先复核待确认人选，再决定是否扩展目标公司。\n\n**下一步**\n\n1. 复核 4 位待确认人选\n2. 根据结果扩展目标公司', references: [
          { type: 'job', id: 154, label: '士兰微 · 技术市场经理', subtitle: '12 位人选 · 4 位待处理' },
          { type: 'candidate', id: 559, label: '衣**', subtitle: 'S1 新增寻访 / 待复核' },
        ] },
      ],
    } })
    return route.fulfill({ json: { ok: true, sessions: [{ session_id: 'agent-shot-task', title: '复盘岗位寻访进度', preview: '建议先复核待确认人选', message_count: 2 }] } })
  })
  await page.goto('/asa-app')
  if ((page.viewportSize()?.width || 0) < 500) await page.getByRole('button', { name: '任务历史' }).click()
  await page.locator('.agent-task-main').filter({ hasText: '复盘岗位寻访进度' }).click()
  await expect(page.locator('.agent-message.assistant')).toBeVisible()
  await expect(page.locator('.agent-object')).toHaveCount(2)
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('agent-conversation.png')
})

test('分析工作区', async ({ page }) => {
  await page.route('**/api/v1/dashboard', route => route.fulfill({ json: overviewDashboard }))
  await page.route('**/api/v1/jobs?**', route => route.fulfill({ json: { items: overviewJobs, total: overviewJobs.length } }))
  await page.route('**/api/v1/candidates?**', route => route.fulfill({ json: { items: overviewCandidates, total: overviewCandidates.length } }))
  await page.route('**/api/v1/workbench?**', route => route.fulfill({ json: overviewWorkbench }))
  await page.route('**/api/v1/analytics/templates', route => route.fulfill({ json: { ok: true, items: [] } }))
  await page.route('**/api/v1/analytics/runs/analysis_fixture', route => route.fulfill({ json: { ok: true, result: overviewAnalysis, duration_ms: 42 } }))
  await page.goto('/asa-app#analysis=analysis_fixture')
  await expect(page.locator('.analysis-workspace')).toBeVisible()
  await expect(page.locator('.analysis-table tbody tr')).toHaveCount(5)
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('analysis.png')
})

test('固定分析管理', async ({ page }) => {
  await mockOverview(page)
  await page.route('**/api/v1/analytics/templates', route => route.fulfill({ json: { ok: true, items: [fixedTemplate] } }))
  await page.route('**/api/v1/analytics/templates/template_fixture/runs', route => route.fulfill({ json: { ok: true, items: [
    { template_run_id: 'tr2', template_id: 'template_fixture', analysis_run_id: 'analysis_fixture', trigger: 'schedule', status: 'completed', started_at: '2026-08-03T01:00:00+00:00', headline: overviewAnalysis.headline },
    { template_run_id: 'tr1', template_id: 'template_fixture', analysis_run_id: 'analysis_previous', trigger: 'manual', status: 'failed', started_at: '2026-08-02T01:00:00+00:00', error: '数据读取超时' },
  ] } }))
  await page.goto('/asa-app')
  await page.getByRole('button', { name: '管理固定分析：每日经营变化' }).click()
  await expect(page.getByRole('dialog', { name: '管理固定分析' })).toBeVisible()
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('fixed-analysis.png')
})

test('固定分析趋势', async ({ page }) => {
  await mockOverview(page)
  await page.route('**/api/v1/analytics/templates', route => route.fulfill({ json: { ok: true, items: [fixedTemplate] } }))
  await page.route('**/api/v1/analytics/runs/analysis_fixture', route => route.fulfill({ json: { ok: true, result: overviewAnalysis, duration_ms: 42, template_id: 'template_fixture' } }))
  await page.route('**/api/v1/analytics/templates/template_fixture/trend', route => route.fulfill({ json: {
    ok: true, template_id: 'template_fixture', name: '每日经营变化', catalog_id: 'operations_overview', run_count: 3, runs: [],
    series: overviewAnalysis.metrics.slice(0, 4).map((metric, index) => ({
      metric_id: metric.id, label: metric.label, unit: metric.unit, latest: metric.value,
      previous: typeof metric.value === 'number' ? metric.value - index - 1 : null,
      delta: typeof metric.value === 'number' ? index + 1 : null, delta_ratio: null,
      points: [
        { run_id: 'a1', at: '2026-08-01', value: typeof metric.value === 'number' ? metric.value - index - 2 : null },
        { run_id: 'a2', at: '2026-08-02', value: typeof metric.value === 'number' ? metric.value - index - 1 : null },
        { run_id: 'analysis_fixture', at: '2026-08-03', value: metric.value },
      ],
    })),
  } }))
  await page.goto('/asa-app')
  await page.getByRole('button', { name: /每日经营变化/ }).first().click()
  await expect(page.getByRole('region', { name: '变化趋势' })).toBeVisible()
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('analysis-trend.png')
})

test('岗位 #154 详情', async ({ page }) => {
  const panel = await openJob(page)
  await expect(panel.locator('.job-funnel')).toBeVisible()
  // 人选数随真实业务增长（T3 第 4 轮新增 4 位），从同实例 API 取期望数，不再钉死
  const job = await (await page.request.get('/api/v1/jobs/154')).json()
  const expected = ((job.job ?? job).candidates ?? []).length
  await expect(panel.locator('.job-candidate-list button')).toHaveCount(expected)
  await expect(panel.locator('.job-candidate-list')).toContainText('唐**')
  // S8 岗位画像区块为异步拉取：等其渲染稳定（就绪或空态）再拍，避免基线抖动
  await expect(panel.locator('.job-profile-section')).toBeVisible()
  await expect(page).toHaveScreenshot('job-154.png')
})

test('工作流详情（blocked + completed_needs_review）', async ({ page }) => {
  const panel = await openWorkflow(page)
  await expect(panel.getByRole('group', { name: '下一步操作' })).toBeVisible()
  // 人选结果加载完成（岗位级口径，人数随轮次增长）+ 执行步骤 5 步全部渲染
  await expect(panel.locator('.workflow-candidates')).toContainText(/岗位已评估 \d+ 人/)
  await expect(panel.locator('.workflow-step')).toHaveCount(5)
  // 渠道漏斗加载完成（第 7 轮有真实漏斗行：猎聘渠道行 + X-SaaS 0 召回归因）
  await expect(panel.locator('.workflow-funnel')).toContainText('猎聘')
  await expect(page).toHaveScreenshot('workflow.png')
})
