import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openJob } from '../support/nav'

skipIfNoBackend()

test('岗位画像纠正后立即回读岗位最近动态', async ({ page }) => {
  const panel = await openJob(page)
  const profile = panel.locator('.job-profile-section')
  const duties = profile.locator('.job-profile-block').filter({ hasText: '职责分布' })
  const item = duties.locator('.job-profile-item').filter({ hasText: '4英寸屏幕电源设计' })

  await expect(item).toBeVisible()
  await item.getByRole('button', { name: '不对', exact: true }).click()

  await expect(profile).toContainText('已记录"4英寸屏幕电源设计"不对')
  await expect(profile).toContainText('顾问已标记不对（1 条')
  await expect(panel.locator('aside .timeline')).toContainText('更新岗位画像（这个岗位实际在干什么）')
  await expect(panel.locator('aside .timeline')).toContainText('顾问已标记 1 条不对')
})
