import type { Page } from '@playwright/test'
import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { WORKFLOW_ID, openWorkflow } from '../support/nav'

skipIfNoBackend()

type NativeWindow = Window & { __asaNativeMessages?: Array<Record<string, unknown>> }

async function installNativeBridge(page: Page) {
  await page.addInitScript(() => {
    const target = window as NativeWindow
    target.__asaNativeMessages = []
    Object.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { asaNative: { postMessage: (message: Record<string, unknown>) => target.__asaNativeMessages?.push(message) } } },
    })
  })
}

async function expectNativeCopilotOpen(page: Page) {
  await expect.poll(() => page.evaluate(() => (window as NativeWindow).__asaNativeMessages || [])).toContainEqual({ type: 'showFloating' })
}

// R12-b：策略修订只在原生 Copilot 浮窗交互。这里模拟 WKWebView bridge，验证两个 App 入口
// 都先发上下文给 Core，再请求原生容器显示浮窗；浏览器页面不再渲染“修改计划”对话框。
test('策略调整：终态入口将工作流上下文交给原生 Copilot', async ({ page }) => {
  await installNativeBridge(page)
  const panel = await openWorkflow(page)
  const contextSaved = page.waitForResponse(response => response.url().endsWith('/api/asa/floating/context') && response.request().method() === 'POST')
  await panel.getByRole('group', { name: '下一步操作' }).getByRole('button', { name: '在 Copilot 中调整策略' }).click()
  await expect((await contextSaved).ok()).toBe(true)
  await expectNativeCopilotOpen(page)
})

test('策略讨论：头部入口同样交给原生 Copilot，不触发旧 revise 接口', async ({ page }) => {
  await installNativeBridge(page)
  let reviseCalls = 0
  await page.route(`**/api/v1/workflows/${WORKFLOW_ID}/revise`, route => { reviseCalls += 1; void route.continue() })
  const panel = await openWorkflow(page)
  await panel.locator('header.detail-head').getByRole('button', { name: '在 Copilot 中讨论策略' }).click()
  await expectNativeCopilotOpen(page)
  expect(reviseCalls).toBe(0)
})
