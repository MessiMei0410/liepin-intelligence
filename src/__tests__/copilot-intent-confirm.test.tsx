import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Copilot } from '../copilot/Copilot'
import { mockResponse } from './helpers'

const messagesUrl = '/api/v1/copilot/messages'
const confirmUrl = '/api/v1/copilot/intents/confirm'

// R9 契约样例：POST /api/v1/copilot/messages 响应携带 pending_intent。
const pendingIntent = {
  kind: 'candidate_action',
  action: 'stop',
  action_label: '停止推进',
  target_scope: 'candidate',
  confidence: 0.91,
  reason: '用户明确要求停止推进',
  candidate: { id: 1, name: '张三', stage: 'S1 待复核', job: '前端工程师', client: 'ACME' },
  confirm_text: '确认停止推进候选人张三（ACME · 前端工程师）？',
  intent_hash: 'hash-abc',
  preflight_token: 'tok-xyz',
  expires_at: '2026-07-22 11:00',
  message: '停止推进张三',
}

const copilotReply = () => mockResponse({ session_id: 's-1', answer: '', pending_intent: pendingIntent })

describe('Copilot 意图确认卡（R9）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    localStorage.clear()
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(confirmUrl)) return mockResponse({ ok: true, candidate_action: { action: 'stop' }, answer: '已停止推进张三。' })
      if (url.includes(messagesUrl)) return copilotReply()
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const openCard = async () => {
    const user = userEvent.setup()
    render(<Copilot context={{ type: 'candidate', id: 1 }} openWorkflow={() => undefined} />)
    await user.type(screen.getByRole('textbox', { name: '向 ASA 提问' }), '停止推进张三')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByText(pendingIntent.confirm_text)
    return { user }
  }

  it('pending_intent 渲染确认卡：confirm_text + 候选人摘要 + 确认/取消按钮', async () => {
    await openCard()
    expect(screen.getByText(pendingIntent.confirm_text)).toBeInTheDocument()
    expect(screen.getByText(/S1 待复核/)).toBeInTheDocument()
    expect(screen.getByText('ACME · 前端工程师')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeEnabled()
    // 渲染卡片本身不触发任何写请求：messages 仅一次，confirm 端点零调用
    //（session_id 更新触发的 history 回读为既有行为，属读请求）。
    const calls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(calls.filter((url) => url.includes(messagesUrl))).toHaveLength(1)
    expect(calls.filter((url) => url.includes(confirmUrl))).toHaveLength(0)
  })

  it('确认提交签名体（hash/token 原样回传 + Idempotency-Key）并插入 answer，卡片进入已确认终态', async () => {
    const { user } = await openCard()
    fetchMock.mockClear()
    await user.click(screen.getByRole('button', { name: '确认' }))
    expect(await screen.findByText('已停止推进张三。')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [input, init] = fetchMock.mock.calls[0]
    expect(String(input)).toContain(confirmUrl)
    const headers = (init as RequestInit).headers as Record<string, string>
    expect(headers['Idempotency-Key']).toContain('web_')
    expect(headers['Idempotency-Key']).toContain(confirmUrl)
    const body = JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>
    expect(body).toMatchObject({
      intent: { kind: 'candidate_action', action: 'stop' },
      intent_hash: 'hash-abc',
      preflight_token: 'tok-xyz',
      candidate_id: 1,
      message: '停止推进张三',
      session_id: 's-1',
    })
    expect(typeof body.request_id).toBe('string')
    expect(await screen.findByText('已确认，操作已执行。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认' })).not.toBeInTheDocument()
  })

  it('取消进入已取消终态且零写请求', async () => {
    const { user } = await openCard()
    fetchMock.mockClear()
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(await screen.findByText('已取消，未执行任何写操作。')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '确认' })).not.toBeInTheDocument()
  })

  it('409 显示状态漂移错误文案', async () => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(confirmUrl)) return mockResponse({ detail: '候选人状态已漂移，preflight 签名失效' }, false, 409)
      if (url.includes(messagesUrl)) return copilotReply()
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const { user } = await openCard()
    await user.click(screen.getByRole('button', { name: '确认' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('候选人状态已漂移，preflight 签名失效')
    expect(screen.queryByRole('button', { name: '确认' })).not.toBeInTheDocument()
    expect(screen.queryByText('已确认，操作已执行。')).not.toBeInTheDocument()
  })
})
