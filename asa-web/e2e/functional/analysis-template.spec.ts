import { expect, skipIfNoBackend, test } from '../support/fixtures'

skipIfNoBackend()

test('固定分析可创建、定时、追踪趋势并删除', async ({ page }) => {
  await page.goto('/asa-app')
  await expect(page.getByRole('heading', { name: '今天从哪里开始？' })).toBeVisible()

  await page.getByRole('button', { name: '新建固定分析' }).click()
  const dialog = page.getByRole('dialog', { name: '新建固定分析' })
  await dialog.getByLabel('名称').fill('E2E 每日经营变化')
  await dialog.getByLabel('统计周期（天）').fill('7')
  await dialog.getByRole('button', { name: '每天' }).click()
  await dialog.getByLabel('启用自动执行').check()
  await dialog.getByRole('button', { name: '创建' }).click()

  const analysisRow = page.locator('.agent-analysis-row', { hasText: 'E2E 每日经营变化' })
  await expect(analysisRow).toBeVisible()
  await analysisRow.locator('button').first().click()
  await expect(page.locator('.analysis-workspace')).toBeVisible()
  await page.getByRole('button', { name: '刷新' }).click()
  await expect(page.getByRole('region', { name: '变化趋势' })).toBeVisible()
  await expect(page.getByText('2 次固定分析')).toBeVisible()

  await page.getByRole('button', { name: '返回' }).click()
  await page.getByRole('button', { name: '管理固定分析：E2E 每日经营变化' }).click()
  const manage = page.getByRole('dialog', { name: '管理固定分析' })
  await expect(manage.getByText('最近 2 次')).toBeVisible()
  await expect(manage.getByText('已完成')).toHaveCount(2)
  await manage.getByRole('button', { name: '删除' }).click()
  await manage.getByRole('button', { name: '确认删除' }).click()
  await expect(page.locator('.agent-analysis-row', { hasText: 'E2E 每日经营变化' })).toHaveCount(0)
})
