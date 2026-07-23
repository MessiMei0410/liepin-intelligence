import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidatePanel } from '../panels/CandidatePanel'
import { candidateDetail, mockResponse } from './helpers'

// S4-5（N5）评分复核快捷记录：顾问结论（尺太严/人不行）作为 note 经既有
// preflight→commit 链路写入候选人事件（action=review，后端既有通道），不新开写路径。

const preflightUrl = '/api/v1/candidate-actions/preflight'
const commitUrl = '/api/v1/candidate-actions/commit'

describe('评分复核快捷记录（S4-5 N5）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(preflightUrl)) return mockResponse({ token: 'tok-review', impact: '候选人关系状态将更新，并写入业务时间线和统一审计。', expires_at: '2026-07-23 10:00' })
      if (url.includes(commitUrl)) return mockResponse({ ok: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const openDialog = async () => {
    const user = userEvent.setup()
    const changed = vi.fn()
    render(<CandidatePanel value={candidateDetail} close={() => undefined} changed={changed} />)
    await user.click(screen.getByRole('button', { name: '评分复核' }))
    const dialog = await screen.findByRole('alertdialog')
    return { user, changed, dialog }
  }

  const commitBody = () => {
    const commitCall = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(commitCall).toBeDefined()
    return JSON.parse(String((commitCall?.[1] as RequestInit).body)) as Record<string, unknown>
  }

  it('预检以 action=review 发起，确认对话框列出尺太严/人不行快捷结论', async () => {
    const { dialog } = await openDialog()
    expect(within(dialog).getByText('评分复核', { selector: 'h3' })).toBeInTheDocument()
    const preflightCall = fetchMock.mock.calls.find(([input]) => String(input).includes(preflightUrl))
    expect(JSON.parse(String((preflightCall?.[1] as RequestInit).body))).toMatchObject({ candidate_id: 1, action: 'review' })
    expect(within(dialog).getByText('复核结论（是尺严还是人不行）')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '尺太严' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '人不行' })).toBeInTheDocument()
    // 备注预填【评分复核】前缀，顾问可再编辑
    expect(within(dialog).getByRole('textbox', { name: '结论备注' })).toHaveValue('【评分复核】')
  })

  it('点选快捷结论后 commit 携带结论 note（经既有 commit 链路）', async () => {
    const { user, changed, dialog } = await openDialog()
    await user.click(within(dialog).getByRole('button', { name: '尺太严' }))
    expect(within(dialog).getByRole('textbox', { name: '结论备注' })).toHaveValue('【评分复核】结论：尺太严，建议校准放宽')
    await user.click(within(dialog).getByRole('button', { name: '确认评分复核' }))
    expect(await screen.findByText('评分复核结论已记录到候选人事件。')).toBeInTheDocument()
    expect(commitBody()).toMatchObject({
      candidate_id: 1,
      action: 'review',
      preflight_token: 'tok-review',
      note: '【评分复核】结论：尺太严，建议校准放宽',
    })
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('结论备注可编辑：人不行结论 + 顾问补充原文提交', async () => {
    const { user, dialog } = await openDialog()
    await user.click(within(dialog).getByRole('button', { name: '人不行' }))
    await user.type(within(dialog).getByRole('textbox', { name: '结论备注' }), '，硬伤年限不足属实')
    await user.click(within(dialog).getByRole('button', { name: '确认评分复核' }))
    expect(await screen.findByText('评分复核结论已记录到候选人事件。')).toBeInTheDocument()
    expect(commitBody()).toMatchObject({
      action: 'review',
      note: '【评分复核】结论：人不行，维持原判，硬伤年限不足属实',
    })
  })

  it('已停止候选人不展示评分复核按钮（既有停止边界不回归）', () => {
    render(<CandidatePanel value={{ ...candidateDetail, is_stopped: true }} close={() => undefined} changed={() => undefined} />)
    expect(screen.getByText('已停止推进')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '评分复核' })).not.toBeInTheDocument()
  })
})
