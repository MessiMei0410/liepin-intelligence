import { expect, skipIfNoBackend, test } from '../support/fixtures'

const JOB_ID = 137
const ADJUSTMENT_ID = 11

skipIfNoBackend()

test('停止备注调整先采纳，策略未产出前不显示为已应用', async ({ page }) => {
  await page.goto(`/asa-app#job=${JOB_ID}`)
  const panel = page.locator('.job-detail-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.detail-head h2')).toContainText('机械高级工程师')

  const adjustments = panel.getByRole('region', { name: '寻访调整' })
  await expect(adjustments).toContainText('1 条待判断')
  const receiptPromise = page.waitForResponse(response =>
    response.request().method() === 'POST'
    && response.url().endsWith(`/api/v1/sourcing-adjustments/${ADJUSTMENT_ID}/confirm`))
  await adjustments.getByRole('button', { name: '采纳调整' }).click()

  const receipt = await receiptPromise
  expect(receipt.ok()).toBe(true)
  const payload = await receipt.json() as {
    status?: string
    accepted_at?: string
    applied_at?: string | null
    applied_round?: number | null
    applied_workflow_id?: string | null
    applied_artifact_id?: string | null
  }
  expect(payload.status).toBe('accepted')
  expect(payload.accepted_at).toBeTruthy()
  expect(payload.applied_at).toBeNull()
  expect(payload.applied_round).toBeNull()
  expect(payload.applied_workflow_id).toBeNull()
  expect(payload.applied_artifact_id).toBeNull()

  const accepted = adjustments.getByRole('region', { name: '已采纳待应用调整' })
  await expect(accepted).toContainText('已采纳，待下轮策略')
  await expect(accepted).toContainText('直观复星医疗器械技术(上海)有限公司')
  await expect(adjustments.getByRole('button', { name: '采纳调整' })).toHaveCount(0)
  await expect(adjustments).not.toContainText('已应用于第')
})

