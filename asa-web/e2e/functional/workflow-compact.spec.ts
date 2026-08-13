import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openWorkflow } from '../support/nav'

skipIfNoBackend()

// 审批锚点：长越科技·机械高级工程师第 8 轮 waiting_approval + 唯一 pending R3。
// 业务侧批准/拒绝该审批后需换成最新 waiting_approval 工作流（2026-08-11 时另有 workflow_f76d2841a152 可替补）。
const APPROVAL_WORKFLOW_ID = 'workflow_a32622d8ff0c'

test('工作流默认轻量展示，策略与人选按需打开独立界面并可返回', async ({ page }) => {
  const compact = await openWorkflow(page)
  await expect(compact.locator('.compact-workflow-steps li')).toHaveCount(5)
  await expect(page.locator('.workflow-strategy')).toHaveCount(0)
  await expect(page.locator('.workflow-candidates')).toHaveCount(0)

  // 查看策略：进入策略独立界面，不挂载人选模块，也不再是完整流程面板。
  await compact.getByRole('button', { name: '查看' }).click()
  await compact.getByRole('menuitem', { name: '寻访策略' }).click()
  const strategyView = page.locator('.workflow-section-dialog')
  await expect(strategyView).toBeVisible()
  await expect(strategyView.locator('.workflow-strategy')).toBeVisible()
  await expect(strategyView.locator('.workflow-candidates')).toHaveCount(0)
  await expect(page.locator('.workflow-panel')).toHaveCount(0)
  await expect(page.locator('.compact-workflow-steps')).toHaveCount(0)

  // 返回步骤摘要后再看人选：人选独立界面只挂载名单模块。
  await strategyView.getByRole('button', { name: '返回' }).click()
  const compactAgain = page.locator('.compact-workflow-dialog:not(.workflow-section-dialog)')
  await expect(compactAgain).toBeVisible()
  await expect(compactAgain.locator('.compact-workflow-steps li')).toHaveCount(5)
  await compactAgain.getByRole('button', { name: '查看' }).click()
  await compactAgain.getByRole('menuitem', { name: '人选名单' }).click()
  const candidatesView = page.locator('.workflow-section-dialog')
  await expect(candidatesView.locator('.workflow-candidates')).toBeVisible()
  await expect(candidatesView.locator('.workflow-strategy')).toHaveCount(0)

  // 二级界面可直接进完整详情（完整流程面板）。
  await candidatesView.getByRole('button', { name: '查看完整详情' }).click()
  const fullPanel = page.locator('.workflow-panel')
  await expect(fullPanel).toBeVisible()
  await expect(fullPanel.locator('.workflow-strategy')).toBeVisible()
  await expect(fullPanel.locator('.workflow-candidates')).toBeVisible()

  // 完整详情与各界面一样有统一关闭按钮，一键关掉整个浮层。
  await fullPanel.getByRole('button', { name: '关闭' }).click()
  await expect(page.locator('.workflow-panel')).toHaveCount(0)
  await expect(page.locator('.compact-workflow-dialog')).toHaveCount(0)
})

test('待审批工作流在轻量浮层直接决策', async ({ page }) => {
  // 该工作流的详情响应约 29MB（多轮审批历史），决策回写 + 详情重拉整体偏慢，按慢用例放宽。
  test.slow()
  await page.goto(`/asa-app#workflow=${APPROVAL_WORKFLOW_ID}`)
  const compact = page.locator('.compact-workflow-dialog')
  // 详情响应约 29MB：全量 e2e 共享隔离 Core 时负载更高，首屏等待放宽到 30s（2026-08-13 全量超时实测）。
  await expect(compact).toBeVisible({ timeout: 30_000 })
  await expect(compact.locator('.compact-workflow-head h2')).toContainText('长越科技')

  // 待审批动作钉在首层：R3 单次授权，文案来自 Core 状态映射。
  const approval = compact.getByRole('region', { name: '待审批操作' })
  await expect(approval).toBeVisible()
  await expect(approval).toContainText('执行多渠道寻访')
  await expect(approval).toContainText('单次授权')

  // 拒绝（不执行）不触发任何外部寻访动作，只回写审批决定（跑在 /tmp 一次性库副本上）。
  const decided = page.waitForResponse(
    response => response.url().includes('/api/v1/approvals/') && response.url().endsWith('/decision'),
    { timeout: 90_000 },
  )
  await approval.getByRole('button', { name: '不执行' }).click()
  await decided
  await expect(compact.getByRole('region', { name: '待审批操作' })).toHaveCount(0, { timeout: 90_000 })
})
