import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { WORKFLOW_ID, openWorkflow } from '../support/nav'

skipIfNoBackend()

// 工作流“修改计划”内联对话框（RevisePlanDialog）：替代原 window.prompt。
// 覆盖两个入口（终态三按钮“调整条件再搜”、头部“修改计划”）、空输入禁提交、取消路径；
// 绝不真实提交（revise 会触发后端 LLM 改写计划）。

test('修改计划：调整条件再搜入口打开，空输入禁提交，Esc 取消', async ({ page }) => {
  const panel = await openWorkflow(page)
  await panel.getByRole('button', { name: '调整条件再搜' }).click()

  const dialog = page.getByRole('dialog', { name: '修改计划' })
  await expect(dialog).toBeVisible()
  const submit = dialog.getByRole('button', { name: '确认修改' })
  await expect(submit).toBeDisabled()

  await dialog.locator('textarea').fill('   ')
  await expect(submit).toBeDisabled()
  await dialog.locator('textarea').fill('提高学历门槛')
  await expect(submit).toBeEnabled()

  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})

test('修改计划：头部入口打开，取消按钮关闭且不发起 revise 请求', async ({ page }) => {
  let reviseCalls = 0
  await page.route(`**/api/v1/workflows/${WORKFLOW_ID}/revise`, (route) => {
    reviseCalls += 1
    void route.continue()
  })

  const panel = await openWorkflow(page)
  await panel.locator('header.detail-head').getByRole('button', { name: '修改计划' }).click()
  const dialog = page.getByRole('dialog', { name: '修改计划' })
  await expect(dialog).toBeVisible()

  await dialog.locator('footer').getByRole('button', { name: '取消' }).click()
  await expect(dialog).toHaveCount(0)
  expect(reviseCalls).toBe(0)
})
