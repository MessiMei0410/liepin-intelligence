import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openWorkflow } from '../support/nav'

skipIfNoBackend()

// R3 业务终态（blocked + completed_needs_review）：渲染中文业务文案与“下一步操作”三按钮，
// 绝不出现 status 英文原形、business_outcome 枚举原形或旧版“工作流需要处理后继续”文案。

test('R3 业务终态：三按钮存在且可用，业务文案中文化', async ({ page }) => {
  const panel = await openWorkflow(page)

  // 业务文案（头部状态与进度区标题同源 mapped.label）
  await expect(panel.getByText('本轮完成，合格人数不足，有待复核人选').first()).toBeVisible()

  const actions = panel.getByRole('group', { name: '下一步操作' })
  await expect(actions.getByRole('button', { name: '复核现有人选' })).toBeEnabled()
  await expect(actions.getByRole('button', { name: '调整条件再搜' })).toBeEnabled()
  await expect(actions.getByRole('button', { name: '结束本轮' })).toBeEnabled()

  // 负向断言：旧文案、英文 status 原形、英文枚举原形均不出现
  await expect(panel.getByText('工作流需要处理后继续')).toHaveCount(0)
  await expect(panel.getByText(/\bblocked\b/)).toHaveCount(0)
  await expect(panel.getByText('completed_needs_review')).toHaveCount(0)
})

test('R3 业务终态：复核现有人选滚动到人选结果区', async ({ page }) => {
  const panel = await openWorkflow(page)
  await panel.getByRole('group', { name: '下一步操作' }).getByRole('button', { name: '复核现有人选' }).click()
  await expect(panel.locator('.workflow-candidates')).toBeInViewport()
  await expect(panel.locator('.workflow-candidates')).toContainText('人选结果')
})
