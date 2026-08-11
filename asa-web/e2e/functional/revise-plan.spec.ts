import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { WORKFLOW_ID, openWorkflowDetail } from '../support/nav'

skipIfNoBackend()

type NativeWindow = Window & { __asaNativeMessages?: Array<Record<string, unknown>> }

async function installNativeProbe(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    const target = window as NativeWindow
    target.__asaNativeMessages = []
    Object.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { asaNative: { postMessage: (message: Record<string, unknown>) => target.__asaNativeMessages?.push(message) } } },
    })
  })
}

async function expectAgentContext(page: import('@playwright/test').Page) {
  await expect(page.locator('.agent-workspace')).toBeVisible()
  await expect(page.locator('.agent-conversation-head')).toContainText('士兰微')
  expect(await page.evaluate(() => (window as NativeWindow).__asaNativeMessages || [])).toEqual([])
}

test('策略调整：终态入口将工作流上下文附着到 Agent', async ({ page }) => {
  await installNativeProbe(page)
  let floatingContextCalls = 0
  page.on('request', request => {
    if (request.url().includes('/api/asa/floating/context')) floatingContextCalls += 1
  })
  const panel = await openWorkflowDetail(page)
  await panel.getByRole('group', { name: '下一步操作' }).getByRole('button', { name: '在 Agent 中调整策略' }).click()
  await expectAgentContext(page)
  expect(floatingContextCalls).toBe(0)
})

test('策略讨论：头部入口进入 Agent，不触发旧 revise 接口', async ({ page }) => {
  await installNativeProbe(page)
  let reviseCalls = 0
  await page.route(`**/api/v1/workflows/${WORKFLOW_ID}/revise`, route => { reviseCalls += 1; void route.continue() })
  const panel = await openWorkflowDetail(page)
  await panel.locator('header.detail-head').getByRole('button', { name: '在 Agent 中讨论策略' }).click()
  await expectAgentContext(page)
  expect(reviseCalls).toBe(0)
})
