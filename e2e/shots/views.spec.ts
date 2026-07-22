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
  // 6 位人选全部渲染
  await expect(panel.locator('.job-candidate-list button')).toHaveCount(6)
  await expect(panel.locator('.job-candidate-list')).toContainText('唐**')
  await expect(page).toHaveScreenshot('job-154.png')
})

test('工作流详情（blocked + completed_needs_review）', async ({ page }) => {
  const panel = await openWorkflow(page)
  await expect(panel.getByRole('group', { name: '下一步操作' })).toBeVisible()
  // 人选结果加载完成（本轮评估 6 人）+ 执行步骤 5 步全部渲染
  await expect(panel.locator('.workflow-candidates')).toContainText('岗位已评估 6 人')
  await expect(panel.locator('.workflow-step')).toHaveCount(5)
  // 渠道漏斗加载完成（该历史轮次无明细，显示回落文案，避免拍到加载中状态）
  await expect(panel.locator('.workflow-funnel')).toContainText('该轮未记录渠道明细')
  await expect(page).toHaveScreenshot('workflow.png')
})
