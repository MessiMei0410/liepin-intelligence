import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openWorkflowDetail } from '../support/nav'

const MISSING_REVIEW_WORKFLOW_ID = 'workflow_ba826dbdccf0'

skipIfNoBackend()

test('策略复盘重建后立即回读工作流事件与产物', async ({ page }) => {
  test.setTimeout(90_000)
  const panel = await openWorkflowDetail(page, MISSING_REVIEW_WORKFLOW_ID)
  const review = panel.getByRole('region', { name: '没成的原因' })

  await expect(review.getByRole('button', { name: '分析没成的原因' })).toBeVisible()
  const rebuildResponsePromise = page.waitForResponse(response =>
    response.request().method() === 'POST' && response.url().endsWith(`/workflows/${MISSING_REVIEW_WORKFLOW_ID}/strategy-review/rebuild`))
  await review.getByRole('button', { name: '分析没成的原因' }).click()
  expect((await rebuildResponsePromise).ok()).toBe(true)

  await expect(review).not.toContainText('这轮还没分析没成的原因')
  await expect(panel.locator('.workflow-detail-events')).toContainText('策略复盘已重算')
  await expect(panel.locator('.workflow-detail-artifacts')).toContainText('没成的原因')
})
