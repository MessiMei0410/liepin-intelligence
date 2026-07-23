import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { MAPPING_WORKFLOW_ID, openWorkflow } from '../support/nav'

skipIfNoBackend()

// S5-2 Mapping 任务卡主流程（隔离实例；正式库副本含真实 artifact mapping_task_workflow_15fc23c21ce8，
// job 154，17 团队 / 10 候选全 pending；该工作流复盘含 escalate_mapping 步）：
// 打开任务卡（17 团队渲染）→ 确认 1 人（破冰出现且含真实线索词）→ 改已接触 → 再确认 1 人 →
// 刷新后状态持久 → 入库按钮存在。JS 原生对话框护栏由 fixtures 自动加载。

test('Mapping 任务卡：团队树渲染 + 确认破冰 + 状态持久 + 入库入口', async ({ page }) => {
  const panel = await openWorkflow(page, MAPPING_WORKFLOW_ID)
  // 决策树 escalate_mapping 步旁入口：已有任务卡 → 按钮直接打开（不重复发起采集）
  const review = panel.getByRole('region', { name: '没成的原因' })
  await review.getByRole('button', { name: '打开 Mapping 任务卡' }).click()

  const card = panel.getByRole('region', { name: 'Mapping 任务卡' })
  await expect(card).toBeVisible()
  // 真实 artifact：17 团队 / 10 候选
  await expect(card.locator('.mapping-team')).toHaveCount(17)
  await expect(card.locator('.mapping-candidate')).toHaveCount(10)
  await expect(card.getByRole('region', { name: '这份名单的效果' })).toContainText('线索有效率')

  // 确认第 1 人（Y**，MPS 论文作者）→ 开场白要点出现且含真实线索词（论文题 MOSFET）
  const first = card.locator('.mapping-candidate', { hasText: 'Y**' }).first()
  await first.getByRole('button', { name: '确认' }).click()
  await expect(first).toContainText('已确认')
  await expect(first).toContainText('开场白要点')
  await expect(first).toContainText('MOSFET')
  await expect(first).toContainText('只读不发送')

  // 改已接触
  await first.getByRole('button', { name: '已接触' }).click()
  await expect(first).toContainText('已接触')

  // 再确认 1 人（K**），留一个 confirmed 供刷新后验证入库入口
  const second = card.locator('.mapping-candidate', { hasText: 'K**' }).first()
  await second.getByRole('button', { name: '确认' }).click()
  await expect(second).toContainText('已确认')

  // 刷新后状态持久：重开任务卡，Y** 已接触、K** 已确认且入库按钮存在
  await page.reload()
  const reopened = page.locator('.workflow-panel')
  await expect(reopened).toBeVisible()
  await expect(reopened.locator('.detail-head h2')).toContainText('士兰微')
  await reopened.getByRole('region', { name: '没成的原因' }).getByRole('button', { name: '打开 Mapping 任务卡' }).click()
  const card2 = reopened.getByRole('region', { name: 'Mapping 任务卡' })
  await expect(card2.locator('.mapping-team')).toHaveCount(17)
  await expect(card2.locator('.mapping-candidate', { hasText: 'Y**' }).first()).toContainText('已接触')
  const secondAgain = card2.locator('.mapping-candidate', { hasText: 'K**' }).first()
  await expect(secondAgain).toContainText('已确认')
  await expect(secondAgain.getByRole('button', { name: '入库' })).toBeVisible()
})
