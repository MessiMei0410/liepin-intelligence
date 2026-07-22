import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidatePanel } from '../main'
import { candidateDetail, mockResponse } from './helpers'

const preflightUrl = '/api/v1/candidate-actions/preflight'
const commitUrl = '/api/v1/candidate-actions/commit'

describe('候选人停止确认层', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(preflightUrl)) return mockResponse({ token: 'tok-1', impact: '将停止推进该候选人', expires_at: '2026-07-22 10:00' })
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
    await user.click(screen.getByRole('button', { name: '停止' }))
    const dialog = await screen.findByRole('alertdialog')
    return { user, changed, dialog }
  }

  it('预检通过后渲染确认对话框', async () => {
    const { dialog } = await openDialog()
    expect(within(dialog).getByText('停止推进', { selector: 'h3' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toContain(preflightUrl)
  })

  it('取消不调用任何 API', async () => {
    const { user, changed, dialog } = await openDialog()
    fetchMock.mockClear()
    await user.click(within(dialog).getAllByRole('button', { name: '取消' })[0])
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(changed).not.toHaveBeenCalled()
  })

  it('确认提交携带 preflight token 并原地刷新', async () => {
    const { user, changed, dialog } = await openDialog()
    await user.click(within(dialog).getByRole('button', { name: '确认停止推进' }))
    expect(await screen.findByText(/候选人状态已更新/)).toBeInTheDocument()
    const commitCall = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(commitCall).toBeDefined()
    const init = commitCall?.[1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ candidate_id: 1, action: 'stop', preflight_token: 'tok-1', note: '' })
    expect(changed).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})
