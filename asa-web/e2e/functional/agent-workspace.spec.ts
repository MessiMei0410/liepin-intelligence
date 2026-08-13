import { expect, skipIfNoBackend, test } from '../support/fixtures'
import { openJob } from '../support/nav'

skipIfNoBackend()

test('Agent 可选择本地附件并以服务端凭据发送', async ({ page }) => {
  let sentBody: Record<string, unknown> = {}
  await page.route('**/api/v1/copilot/stream', async route => {
    sentBody = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'event: context\ndata: {"session_id":"agent-file-e2e"}\n\nevent: text\ndata: {"content":"附件已读取"}\n\nevent: done\ndata: {"ok":true,"session_id":"agent-file-e2e","answer":"附件已读取"}\n\n',
    })
  })
  await page.goto('/asa-app?surface=copilot')

  await page.getByLabel('选择附件').setInputFiles({
    name: '探针台CPO需求.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('客户：长川科技\n方向：探针台 CPO\n优先级：急', 'utf8'),
  })
  await expect(page.getByLabel('待发送附件')).toContainText('已读取附件正文')
  await page.getByRole('button', { name: '发送' }).click()
  await expect(page.getByText('附件已读取')).toBeVisible()

  const context = sentBody.context as { uploaded_attachments?: Array<Record<string, unknown>> }
  expect(context.uploaded_attachments).toHaveLength(1)
  expect(context.uploaded_attachments?.[0].attachment_id).toMatch(/^att_[0-9a-f]+$/)
  expect(String(context.uploaded_attachments?.[0].access_token || '').length).toBeGreaterThan(20)
  expect(context.uploaded_attachments?.[0]).not.toHaveProperty('extracted_text')
})

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
  const objectLayout = await page.locator('.agent-message').last().evaluate((node) => {
    const message = node as HTMLElement
    const content = message.querySelector('.agent-message-content') as HTMLElement | null
    const object = message.querySelector('.agent-object') as HTMLElement | null
    return {
      messageWidth: message.getBoundingClientRect().width,
      contentWidth: content?.getBoundingClientRect().width || 0,
      objectWidth: object?.getBoundingClientRect().width || 0,
    }
  })
  expect(objectLayout.objectWidth).toBeGreaterThan(200)
  expect(Math.abs(objectLayout.objectWidth - objectLayout.contentWidth)).toBeLessThanOrEqual(1)
  expect(requestId).toMatch(/^agent_/)
  expect(idempotencyKey).toContain(requestId)

  await page.reload()
  await expect(page.locator('.agent-message-content').getByText('已定位到关联人选。')).toBeVisible()
  await expect(page.locator('.agent-conversation-head')).toContainText('衣**')
  await page.getByRole('button', { name: '展开衣**' }).click()
  const candidateConsole = page.getByRole('region', { name: '候选人决策台' })
  await expect(candidateConsole).toContainText('当前经历')
  await expect(candidateConsole).toContainText('目标岗位')
})

test('Agent 寻访结果卡铺满消息内容列', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('asaAgentSessionId', 'agent-sourcing-result-layout'))
  await page.route('**/api/v1/copilot/sessions**', route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/agent-sourcing-result-layout')) return route.fulfill({ json: {
      ok: true,
      session_id: 'agent-sourcing-result-layout',
      business_focus: { context: { type: 'job', id: 154, client: '士兰微', job: '电源专家' } },
      messages: [{
        role: 'assistant',
        content: '第 3 轮寻访已完成。',
        action_card: {
          type: 'sourcing_result',
          title: '寻访结果：士兰微 · 电源专家 · 第3轮',
          context: { type: 'workflow', id: 'workflow-e2e-result' },
          summary: {
            workflow_id: 'workflow-e2e-result', round: 3, client: '士兰微', job: '电源专家', status: 'completed',
            assessed_count: 50, successful_count: 47, failed_count: 3, total_assessed_in_job: 138,
            recommendation_breakdown: { recommended: 0, verify_first: 18, not_recommended: 120 },
            top_candidates: [], next_actions: [{ type: 'review_candidates', label: '复核现有人选' }],
          },
        },
      }],
    } })
    return route.fulfill({ json: { ok: true, sessions: [] } })
  })

  await page.goto('/asa-app')
  const card = page.getByRole('region', { name: '寻访结果：士兰微 · 电源专家 · 第3轮' })
  await expect(card).toBeVisible()
  const layout = await card.evaluate((node) => {
    const element = node as HTMLElement
    const content = element.parentElement?.querySelector('.agent-message-content') as HTMLElement | null
    return {
      cardWidth: element.getBoundingClientRect().width,
      contentWidth: content?.getBoundingClientRect().width || 0,
      gridColumn: getComputedStyle(element).gridColumn,
    }
  })
  expect(layout.cardWidth).toBeGreaterThan(200)
  expect(Math.abs(layout.cardWidth - layout.contentWidth)).toBeLessThanOrEqual(1)
  // 消息行 3 列（角色 | 时间 | 内容），卡片在内容列。
  expect(layout.gridColumn).toBe('3')
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
  await page.addInitScript(() => {
    localStorage.setItem('asaAgentSessionId', 'agent-manage-task')
    // 任务栏默认折叠：e2e 任务管理断言依赖展开态，显式设置偏好。
    localStorage.setItem('asaTaskRailCollapsed', '0')
  })
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
    // 搜索已改为服务端语义：前端会带 q 参数，mock 按 title/preview 过滤以模拟 Core 行为。
    const query = (url.searchParams.get('q') || '').trim()
    const all = [
      { session_id: 'agent-manage-task', title, preview: '继续补充候选人', message_count: 2, business_focus: focus },
      { session_id: 'agent-other-task', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
    ]
    const sessions = archived ? [] : query ? all.filter(item => item.title.includes(query) || item.preview.includes(query)) : all
    return route.fulfill({ json: { ok: true, sessions } })
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

test('自然语言归档全部任务走任务管理确认，不进入工作流', async ({ page }) => {
  let archiveAllCalls = 0
  let streamCalls = 0
  let archived = false
  await page.route('**/api/v1/copilot/sessions**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.endsWith('/archive-all') && request.method() === 'POST') {
      archiveAllCalls += 1
      archived = true
      return route.fulfill({ json: { ok: true, archived_count: 2, session_ids: ['bulk-1', 'bulk-2'] } })
    }
    return route.fulfill({ json: { ok: true, sessions: archived ? [] : [
      { session_id: 'bulk-1', title: '士兰微寻访', preview: '继续找人', message_count: 2 },
      { session_id: 'bulk-2', title: '长越岗位分析', preview: '分析完成', message_count: 4 },
    ] } })
  })
  await page.route('**/api/v1/copilot/stream', route => {
    streamCalls += 1
    return route.fulfill({ status: 500, json: { error: '不应进入工作流' } })
  })

  await page.goto('/asa-app')
  await page.getByLabel('Agent 消息').fill('归档右侧所有任务')
  await page.getByRole('button', { name: '发送' }).click()
  const dialog = page.getByRole('alertdialog', { name: '归档全部任务' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('消息、业务焦点与审计记录')

  await dialog.getByRole('button', { name: '确认全部归档' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByText('士兰微寻访')).toHaveCount(0)
  await expect(page.getByText('长越岗位分析')).toHaveCount(0)
  expect(archiveAllCalls).toBe(1)
  expect(streamCalls).toBe(0)
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
  await page.getByRole('button', { name: '关闭任务历史' }).click()
  const composer = await page.locator('.agent-composer').boundingBox()
  expect(composer).not.toBeNull()
  expect(composer!.y + composer!.height).toBeLessThanOrEqual(700 - 58)
})

test('模型输出审计抽屉可读取调用状态且窄屏不溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await page.route('**/api/v1/agent/model-audit**', route => route.fulfill({ json: {
    ok: true,
    summary: { total: 2, failed: 1, fallback: 1, avg_duration_ms: 640 },
    items: [{
      call_id: 'e2e-model-call', operation: 'assess_risks', provider: 'api.deepseek.com', model: 'deepseek-v4-flash',
      status: 'failed', validation_status: 'failed', fallback_used: 1, duration_ms: 720,
      input_tokens: 88, output_tokens: 12, request_hash: 'abcdef1234567890',
      request_preview: 'JSON 对象；字段：candidate, job', response_preview: '文本；8 字符',
      error: '模型没有返回合法 JSON', created_at: '2026-08-04 10:00:00',
    }],
  } }))
  await page.route('**/api/v1/copilot/sessions**', route => route.fulfill({ json: { ok: true, sessions: [] } }))
  await page.goto('/asa-app')
  await page.getByRole('button', { name: '模型输出审计' }).click()
  const audit = page.getByRole('complementary', { name: '模型输出审计' })
  await expect(audit).toBeVisible()
  await expect(audit).toContainText('模型失败，已规则降级')
  await expect(audit).toContainText('结构校验失败')
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})

test('人才雷达可从 Agent 首页展开且窄屏不产生页面级横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await page.route('**/api/v1/radar/scans/latest', route => route.fulfill({ json: {
    ok: true,
    radar_scan: {
      scan_date: '2026-08-04', stats: {},
      signals: [{ company: '示例科技', type: 'hiring', summary: '研发岗位增加', as_of: '2026-08-04', source_urls: ['https://example.com'], confidence: 'medium', linked_action: 'mapping' }],
      ranking: [{ company: '示例科技', score: 82, reason: '招聘异动', signal_count: 1 }],
    },
  } }))
  await page.route('**/api/v1/copilot/sessions**', route => route.fulfill({ json: { ok: true, sessions: [] } }))

  await page.goto('/asa-app')
  await page.getByRole('button', { name: '人才雷达' }).click()
  await expect(page.getByRole('region', { name: '人才雷达' })).toContainText('示例科技')

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})

test('Agent 输入框在长对话中保持可见且 Markdown 正确排版', async ({ page }) => {
  await page.setViewportSize({ width: 1310, height: 740 })
  await page.addInitScript(() => localStorage.setItem('asaAgentSessionId', 'agent-layout-task'))
  await page.route('**/api/v1/copilot/sessions**', route => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/agent-layout-task')) return route.fulfill({ json: {
      ok: true,
      session_id: 'agent-layout-task',
      business_focus: null,
      messages: Array.from({ length: 12 }, (_, index) => ({
        role: index % 2 ? 'assistant' : 'user',
        content: index === 11 ? '## 核心结论\n\n**优先处理**\n\n1. 核验候选人\n2. 跟进回复' : `历史消息 ${index + 1}\n\n用于验证长对话滚动区域。`,
      })),
    } })
    return route.fulfill({ json: { ok: true, sessions: [] } })
  })

  await page.goto('/asa-app')
  await expect(page.getByRole('heading', { name: '核心结论' })).toBeVisible()
  await expect(page.getByLabel('Agent 消息')).toBeVisible()
  const composer = await page.locator('.agent-composer').boundingBox()
  const viewport = page.viewportSize()
  expect(composer).not.toBeNull()
  expect(viewport).not.toBeNull()
  expect(composer!.y + composer!.height).toBeLessThanOrEqual(viewport!.height)
  await expect(page.locator('.agent-message-content').last()).not.toContainText('**')
})
