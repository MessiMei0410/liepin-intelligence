import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WriteConfirmationCard } from '../agent/AgentInteractionCards'
import { mockResponse } from './helpers'

// 批量口径便签确认卡（filter_note_batch，多岗位一张卡）：一次列出全部 N 项，
// 用户点一次确认全部生效（activate 单 token → batch commit）；超过 6 条折叠；
// expired/409 漂移支持「重新预检」换新 token 回到待确认（与单卡 #90 同模式）。

const activateUrl = '/api/v1/write-confirmations/activate'
const batchCommitUrl = '/api/v1/jobs/filter-notes/batch'
const batchPreflightUrl = '/api/v1/jobs/filter-notes/batch-preflight'
const recordTurnUrl = '/api/v1/copilot/sessions/record-turn'

// batch-preflight 路径以 batch 为前缀，includes 匹配 commit URL 时会误中预检请求，需排除。
const isCommitCall = (input: unknown) => String(input).includes(batchCommitUrl) && !String(input).includes(batchPreflightUrl)

const twoJobRequest = {
  kind: 'filter_note_batch',
  preflight_token: 'tok-batch-1',
  expires_at: '2099-01-01T00:00:00',
  action: 'job_filter_note_batch',
  items: [
    { job_id: 137, job: { id: 137, title: '机械高级工程师', client: '长越科技' }, note: '六自由度运动台作为大加分项', previous_note: '' },
    { job_id: 138, job: { id: 138, title: '软件高级工程师', client: '长越科技' }, note: '3-5 自由度为次优先', previous_note: '旧口径' },
  ],
  impact: '确认后一次性保存 2 个岗位的筛选口径便签。',
  client_request_id: 'agent_req-b1',
}

const eightJobRequest = {
  ...twoJobRequest,
  items: Array.from({ length: 8 }, (_, i) => ({
    job_id: 200 + i,
    job: { id: 200 + i, title: `岗位${i + 1}`, client: '长越科技' },
    note: `口径便签 ${i + 1}`,
    previous_note: '',
  })),
}

describe('批量口径便签确认卡（filter_note_batch）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(batchPreflightUrl)) return mockResponse({ ok: true, token: 'tok-batch-2', expires_at: '2099-01-02T00:00:00' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(batchCommitUrl)) {
        return mockResponse({
          ok: true, total: 2, saved: 2, already_saved: 0,
          results: [
            { job_id: 137, note: '六自由度运动台作为大加分项', already_saved: false },
            { job_id: 138, note: '3-5 自由度为次优先', already_saved: false },
          ],
        })
      }
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染批量待确认卡：标题带岗位数，逐项列出客户/岗位 + 便签摘要', () => {
    render(<WriteConfirmationCard request={twoJobRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('批量保存口径便签（2 个岗位）')
    expect(card).toHaveTextContent('长越科技 / 机械高级工程师')
    expect(card).toHaveTextContent('六自由度运动台作为大加分项')
    expect(card).toHaveTextContent('长越科技 / 软件高级工程师')
    expect(card).toHaveTextContent('3-5 自由度为次优先')
    expect(card).toHaveTextContent('（当前：旧口径）')
    expect(screen.getByRole('button', { name: '确认执行' })).toBeEnabled()
  })

  it('超过 6 条折叠：默认只展示前 6 条，点击展开全部', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={eightJobRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('批量保存口径便签（8 个岗位）')
    expect(card).toHaveTextContent('口径便签 6')
    expect(card).not.toHaveTextContent('口径便签 7')
    await user.click(screen.getByRole('button', { name: '展开全部 8 条' }))
    expect(card).toHaveTextContent('口径便签 8')
    await user.click(screen.getByRole('button', { name: '收起' }))
    expect(card).not.toHaveTextContent('口径便签 7')
  })

  it('确认：先 activate 后 batch commit（单 token + 整批 items），回执含逐项结果并回填 confirmed', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={twoJobRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    const receipt = await screen.findByRole('region', { name: '写入执行回执' })
    expect(receipt).toHaveTextContent('已保存 2 个岗位的口径便签')
    const perItem = within(receipt).getByRole('list', { name: '逐项结果' })
    expect(perItem).toHaveTextContent('长越科技 / 机械高级工程师：已保存')
    expect(perItem).toHaveTextContent('长越科技 / 软件高级工程师：已保存')

    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    const activateIndex = urls.findIndex(url => url.includes(activateUrl))
    const commitIndex = urls.findIndex((_, i) => isCommitCall(fetchMock.mock.calls[i][0]))
    expect(activateIndex).toBeGreaterThanOrEqual(0)
    expect(commitIndex).toBeGreaterThan(activateIndex)
    expect(JSON.parse(String((fetchMock.mock.calls[activateIndex][1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-batch-1' })
    expect(JSON.parse(String((fetchMock.mock.calls[commitIndex][1] as RequestInit).body))).toMatchObject({
      preflight_token: 'tok-batch-1',
      items: [
        { job_id: 137, note: '六自由度运动台作为大加分项' },
        { job_id: 138, note: '3-5 自由度为次优先' },
      ],
    })
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(backfill).toBeDefined()
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({
        session_id: 'asa-s1', request_id: 'agent_req-b1',
        confirm_result: { state: 'confirmed' },
      })
    })
  })

  it('取消：零写请求，展示已取消并回填 cancelled 终态', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={twoJobRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(await screen.findByRole('region', { name: '写入确认已取消' })).toHaveTextContent('已取消，未写入 ASA')
    expect(fetchMock.mock.calls.filter(([input]) => isCommitCall(input))).toHaveLength(0)
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes(activateUrl))).toHaveLength(0)
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({ confirm_result: { state: 'cancelled' } })
    })
  })

  it('过期卡：出「重新预检」按钮，换新 token 回到待确认，确认走新 token', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={{ ...twoJobRequest, expires_at: '2020-01-01T00:00:00' }} sessionId="asa-s1" />)
    expect(await screen.findByRole('region', { name: '写入确认已过期' })).toHaveTextContent('确认请求已过期')
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    // 重预检成功回到待确认；预检请求带整批 items
    const preflightCall = fetchMock.mock.calls.find(([input]) => String(input).includes(batchPreflightUrl))
    expect(JSON.parse(String((preflightCall?.[1] as RequestInit).body))).toMatchObject({
      items: [
        { job_id: 137, note: '六自由度运动台作为大加分项' },
        { job_id: 138, note: '3-5 自由度为次优先' },
      ],
    })
    await user.click(await screen.findByRole('button', { name: '确认执行' }))
    await screen.findByRole('region', { name: '写入执行回执' })
    const commitCall = fetchMock.mock.calls.find(([input]) => isCommitCall(input))
    expect(JSON.parse(String((commitCall?.[1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-batch-2' })
  })

  it('409 漂移：展示服务端错误并可重新预检回待确认', async () => {
    const user = userEvent.setup()
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(batchPreflightUrl)) return mockResponse({ ok: true, token: 'tok-batch-3', expires_at: '2099-01-02T00:00:00' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(batchCommitUrl)) return mockResponse({ detail: '岗位不存在（#138），本次批量未写入任何便签' }, false, 409)
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    render(<WriteConfirmationCard request={twoJobRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('本次批量未写入任何便签')
    // 漂移后可重新预检换新 token 回到待确认
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('button', { name: '确认执行' })).toBeEnabled()
  })
})
