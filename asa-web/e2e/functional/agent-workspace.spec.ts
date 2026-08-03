import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openJob } from '../support/nav'

skipIfNoBackend()

test('Agent 发送任务、渲染对象卡并在重载后恢复', async ({ page }) => {
  let created = false
  let requestId = ''
  let idempotencyKey = ''
  const restoredMessages = [
    { role: 'user', content: '继续推进这个人选', context: { type: 'page', page: 'agent' } },
    { role: 'assistant', content: '已定位到关联人选。', references: [{ type: 'candidate', id: 559, label: '衣**' }] },
  ]

  await page.route('**/api/v1/copilot/sessions**', route => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/agent-e2e-task')) {
      return route.fulfill({ json: { ok: true, session_id: 'agent-e2e-task', messages: restoredMessages, business_focus: { context: { type: 'candidate', id: 559, candidate: '衣**' } } } })
    }
    return route.fulfill({ json: { ok: true, sessions: created ? [{ session_id: 'agent-e2e-task', title: '继续推进这个人选', preview: '已定位到关联人选。', message_count: 2 }] : [] } })
  })
  await page.route('**/api/v1/copilot/stream', async route => {
    const body = route.request().postDataJSON() as { request_id: string }
    requestId = body.request_id
    idempotencyKey = route.request().headers()['idempotency-key'] || ''
    created = true
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: [
        'event: context\ndata: {"session_id":"agent-e2e-task"}\n\n',
        'event: text\ndata: {"content":"已定位"}\n\n',
        'event: done\ndata: {"ok":true,"session_id":"agent-e2e-task","answer":"已定位到关联人选。","references":[{"type":"candidate","id":559,"label":"衣**"}],"business_focus":{"context":{"type":"candidate","id":559,"candidate":"衣**"}}}\n\n',
      ].join(''),
    })
  })

  await page.goto('/asa-app')
  await page.getByLabel('Agent 消息').fill('继续推进这个人选')
  await page.getByRole('button', { name: '发送' }).click()
  await expect(page.locator('.agent-message-content').getByText('已定位到关联人选。')).toBeVisible()
  await expect(page.getByRole('button', { name: '展开衣**' })).toBeVisible()
  expect(requestId).toMatch(/^agent_/)
  expect(idempotencyKey).toContain(requestId)

  await page.reload()
  await expect(page.locator('.agent-message-content').getByText('已定位到关联人选。')).toBeVisible()
  await expect(page.locator('.agent-conversation-head')).toContainText('衣**')
  await page.getByRole('button', { name: '展开衣**' }).click()
  await expect(page.locator('.agent-object-body')).toContainText('推进阶段')
})

test('岗位详情的“交给 Agent”显式附着岗位上下文', async ({ page }) => {
  const panel = await openJob(page)
  await panel.getByRole('button', { name: '交给 Agent' }).click()
  await expect(page.locator('.agent-workspace')).toBeVisible()
  await expect(page.locator('.agent-conversation-head')).toContainText('士兰微')
})

test('Agent 任务可搜索、改名、归档并解除业务焦点', async ({ page }) => {
  let title = '士兰微寻访'
  let archived = false
  let focus: Record<string, unknown> | null = { context: { type: 'job', id: 154 }, client: '士兰微', job: { title: '电源专家' } }
  const patches: Array<Record<string, unknown>> = []
  await page.addInitScript(() => localStorage.setItem('asaAgentSessionId', 'agent-manage-task'))
  await page.route('**/api/v1/copilot/sessions**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'PATCH') {
      const body = request.postDataJSON() as Record<string, unknown>
      patches.push(body)
      if (typeof body.title === 'string') title = body.title
      if (body.clear_focus) focus = null
      if (body.archived) archived = true
      return route.fulfill({ json: { ok: true, session_id: 'agent-manage-task', title, archived, business_focus: focus } })
    }
    if (url.pathname.endsWith('/agent-manage-task')) {
      return route.fulfill({ json: { ok: true, session_id: 'agent-manage-task', messages: [], business_focus: focus } })
    }
    return route.fulfill({ json: { ok: true, sessions: archived ? [] : [
      { session_id: 'agent-manage-task', title, preview: '继续补充候选人', message_count: 2, business_focus: focus },
      { session_id: 'agent-other-task', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
    ] } })
  })

  await page.goto('/asa-app')
  await page.getByRole('button', { name: /士兰微寻访 最近：继续补充候选人/ }).click()
  await expect(page.getByRole('status', { name: '当前任务焦点' })).toContainText('士兰微 / 电源专家')
  await page.getByRole('button', { name: '解除任务焦点' }).click()
  await expect(page.getByRole('status', { name: '当前任务焦点' })).toHaveCount(0)

  await page.getByLabel('搜索任务').fill('士兰微')
  await expect(page.getByText('长越岗位分析')).toHaveCount(0)
  await page.getByRole('button', { name: '重命名任务：士兰微寻访' }).click()
  await page.getByLabel('任务名称').fill('士兰微电源寻访')
  await page.getByRole('form', { name: '重命名任务' }).getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('button', { name: '归档任务：士兰微电源寻访' })).toBeVisible()
  await page.getByRole('button', { name: '归档任务：士兰微电源寻访' }).click()
  await page.getByRole('button', { name: '确认归档任务：士兰微电源寻访' }).click()
  await expect(page.getByText('士兰微电源寻访')).toHaveCount(0)

  expect(patches.some(item => item.clear_focus === true)).toBe(true)
  expect(patches.some(item => item.title === '士兰微电源寻访')).toBe(true)
  expect(patches.some(item => item.archived === true)).toBe(true)
  expect(patches.every(item => typeof item.request_id === 'string')).toBe(true)
})

test('390px 任务抽屉没有横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await page.route('**/api/v1/copilot/sessions**', route => route.fulfill({ json: { ok: true, sessions: [
    { session_id: 'agent-mobile-task', title: '很长的士兰微电源专家持续寻访与候选人复核任务', preview: '继续补充候选人并复核现有名单', message_count: 12 },
  ] } }))
  await page.goto('/asa-app')
  await page.getByRole('button', { name: '任务历史' }).click()
  await expect(page.getByRole('complementary', { name: '任务历史' })).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})
