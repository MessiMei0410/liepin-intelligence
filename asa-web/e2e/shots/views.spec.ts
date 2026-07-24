import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openJob, openWorkflow } from '../support/nav'

skipIfNoBackend()

// R6 截图回归：同一组用例跑两个 project——shots-desktop（1440×900）与
// shots-floating（390×700，模拟浮窗宽度）。基线 png 提交于 e2e/snapshots/。
// 等待条件保证异步数据全部落盘后再拍，避免基线抖动。

test('总览', async ({ page }) => {
  await page.goto('/asa-app')
  await expect(page.locator('header.topbar')).toContainText('ASA Agent 在线')
  await expect(page.locator('.metrics')).toBeVisible()
  await expect(page.locator('.work-row').first()).toBeVisible()
  await expect(page.locator('.overview-grid')).toBeVisible()
  // 最近更新人选表格加载完成
  await expect(page.locator('table tbody tr').first()).toBeVisible()
  await expect(page).toHaveScreenshot('overview.png')
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
