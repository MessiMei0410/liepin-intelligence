import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { CANDIDATE_ID, STAGE_BEFORE, STAGE_STOPPED, openCandidateFromJob } from '../support/nav'
import type { APIRequestContext } from '@playwright/test'

skipIfNoBackend()

// 候选人动作确认层三态（R2）：取消无写入 / commit 失败保留对话框与错误文案 / 成功原地阶段变化。
// 三用例共享同一在推人选 #559，按声明顺序串行执行（cancel → failure → success）。

async function candidateStage(request: APIRequestContext, id: number): Promise<string> {
  const response = await request.get(`/api/v1/candidates/${id}`)
  expect(response.ok()).toBeTruthy()
  const body = (await response.json()) as { candidate: { clean_stage?: string } }
  return String(body.candidate.clean_stage || '')
}

test('停止候选人：取消确认对话框，不产生任何写入', async ({ page, request }) => {
  const panel = await openCandidateFromJob(page)
  await expect(panel.locator('.resume-hero')).toContainText(STAGE_BEFORE)

  await panel.getByRole('button', { name: '停止', exact: true }).click()
  const dialog = page.getByRole('alertdialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('#candidate-action-title')).toHaveText('停止推进')
  await expect(dialog).toContainText('衣**')
  await expect(dialog).toContainText(STAGE_BEFORE)
  await expect(dialog).toContainText('预检已通过')

  await dialog.locator('footer').getByRole('button', { name: '取消' }).click()
  await expect(dialog).toHaveCount(0)

  // 面板与数据库双重确认：阶段保持 S1，未出现停止痕迹。
  await expect(panel.locator('.resume-hero')).toContainText(STAGE_BEFORE)
  await expect(panel.locator('.detail-actions')).not.toContainText('已停止推进')
  expect(await candidateStage(request, CANDIDATE_ID)).toContain('S1')
})

test('停止候选人：commit 返回 500，对话框保留并显示错误文案', async ({ page, request }) => {
  await page.route('**/api/v1/candidate-actions/commit', (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'E2E 模拟提交失败' }),
    }),
  )

  const panel = await openCandidateFromJob(page)
  await panel.getByRole('button', { name: '停止', exact: true }).click()
  const dialog = page.getByRole('alertdialog')
  await expect(dialog).toBeVisible()

  await dialog.getByRole('button', { name: '确认停止推进' }).click()

  // 失败态：对话框不关闭，对话框内与面板顶部均给出可读错误。
  await expect(dialog.locator('.action-dialog-error')).toContainText('E2E 模拟提交失败')
  await expect(dialog).toBeVisible()
  await expect(panel.locator('.candidate-action-feedback.error')).toContainText('E2E 模拟提交失败')

  // 失败不产生写入：数据库阶段保持 S1。
  expect(await candidateStage(request, CANDIDATE_ID)).toContain('S1')
})

test('停止候选人：确认后原地阶段变化并提示成功', async ({ page, request }) => {
  const panel = await openCandidateFromJob(page)
  await panel.getByRole('button', { name: '停止', exact: true }).click()
  const dialog = page.getByRole('alertdialog')
  await expect(dialog).toBeVisible()

  // R10：停止原因枚举下拉（默认“其他”）+ 选填备注
  await dialog.getByRole('combobox', { name: '停止原因' }).selectOption({ label: '方向不符' })
  await dialog.getByPlaceholder('补充说明（选填）').fill('E2E 验证停止流程')
  await dialog.getByRole('button', { name: '确认停止推进' }).click()

  // 全套件并行跑时其他用例可能已停止过该候选人（共享同一份 DB 副本），此时 commit 走幂等重放，
  // 文案为“此前已完成，已同步当前候选人状态”——两种成功路径都接受，行为断言（H5/已停止推进）不变。
  // 并行负载下 commit+刷新可能超过默认 10s 断言窗口，放宽到 20s。
  await expect(panel.locator('.candidate-action-feedback.success')).toContainText(/停止推进(此前)?已完成/, { timeout: 20_000 })
  await expect(dialog).toHaveCount(0)

  // 原地变化：面板头部动作区换成“已停止推进 · 方向不符”，当前阶段变为 H5。
  await expect(panel.locator('.detail-actions')).toContainText('已停止推进')
  await expect(panel.locator('.detail-actions')).toContainText('方向不符')
  await expect(panel.locator('.resume-hero')).toContainText(STAGE_STOPPED)
  expect(await candidateStage(request, CANDIDATE_ID)).toContain('H5')
})
