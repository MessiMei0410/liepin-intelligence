import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { MAPPING_WORKFLOW_ID, openWorkflowDetail } from '../support/nav'

skipIfNoBackend()

// S5-2 Mapping 任务卡主流程（隔离实例；正式库副本含真实 artifact mapping_task_workflow_15fc23c21ce8，
// job 154，17 团队 / 至少 2 名可操作候选；该工作流复盘含 escalate_mapping 步）：
// 打开任务卡（17 团队渲染）→ 确认 1 人（破冰区块出现）→ 改已接触 → 再确认 1 人 →
// 刷新后状态持久 → 入库按钮存在。JS 原生对话框护栏由 fixtures 自动加载。
// 注意：正式库人选状态随业务推进漂移（顾问真实使用会确认/接触候选人），选人目标按
// 「当前有确认按钮的卡」（pending）动态确定并以序号跟踪，不再钉人名/既有状态；
// 破冰内容质量随真实数据波动，结构断言只到「开场白要点」区块，内容词由 Vitest mock 覆盖。

test('Mapping 任务卡：团队树渲染 + 确认破冰 + 状态持久 + 入库入口', async ({ page }) => {
  const panel = await openWorkflowDetail(page, MAPPING_WORKFLOW_ID)
  // 决策树 escalate_mapping 步旁入口：已有任务卡 → 按钮直接打开（不重复发起采集）
  const review = panel.getByRole('region', { name: '没成的原因' })
  await review.getByRole('button', { name: '打开 Mapping 任务卡' }).click()

  const card = panel.getByRole('region', { name: 'Mapping 任务卡' })
  await expect(card).toBeVisible()
  // 真实 artifact 的候选池会随顾问补充而增长，数量不应成为业务回归前置条件。
  await expect(card.locator('.mapping-team')).toHaveCount(17)
  await expect(card.locator('.mapping-candidate')).not.toHaveCount(0)
  expect(await card.locator('.mapping-candidate').count()).toBeGreaterThanOrEqual(2)
  await expect(card.getByRole('region', { name: '这份名单的效果' })).toContainText('线索有效率')

  // 按「有确认按钮的卡」找 pending 人选（确认/已接触不改变卡片顺序，序号跨刷新稳定）
  const candidates = card.locator('.mapping-candidate')
  const pendingIndexes: number[] = []
  for (let index = 0; index < (await candidates.count()); index += 1) {
    if (await candidates.nth(index).getByRole('button', { name: '确认' }).count()) pendingIndexes.push(index)
  }
  expect(pendingIndexes.length).toBeGreaterThanOrEqual(2)

  // 确认第 1 位 pending 人选 → 已确认，破冰区块出现（要点或「没生成」原因，二者皆含该词）
  const first = candidates.nth(pendingIndexes[0])
  await first.getByRole('button', { name: '确认' }).click()
  await expect(first).toContainText('已确认')
  await expect(first).toContainText('开场白要点')

  // 改已接触
  await first.getByRole('button', { name: '已接触' }).click()
  await expect(first).toContainText('已接触')

  // 再确认 1 人，留一个 confirmed 供刷新后验证入库入口
  const second = candidates.nth(pendingIndexes[1])
  await second.getByRole('button', { name: '确认' }).click()
  await expect(second).toContainText('已确认')

  // 刷新后状态持久：重开任务卡，第 1 人已接触、第 2 人已确认且入库按钮存在
  await page.reload()
  const compact = page.locator('.compact-workflow-dialog')
  await expect(compact).toBeVisible()
  await compact.getByRole('button', { name: '查看' }).click()
  await compact.getByRole('menuitem', { name: '完整详情' }).click()
  const reopened = page.locator('.workflow-panel')
  await expect(reopened).toBeVisible()
  await reopened.getByRole('region', { name: '没成的原因' }).getByRole('button', { name: '打开 Mapping 任务卡' }).click()
  const card2 = reopened.getByRole('region', { name: 'Mapping 任务卡' })
  await expect(card2.locator('.mapping-team')).toHaveCount(17)
  const firstAgain = card2.locator('.mapping-candidate').nth(pendingIndexes[0])
  await expect(firstAgain).toContainText('已接触')
  const secondAgain = card2.locator('.mapping-candidate').nth(pendingIndexes[1])
  await expect(secondAgain).toContainText('已确认')
  await expect(secondAgain.getByRole('button', { name: '入库' })).toBeVisible()
})
