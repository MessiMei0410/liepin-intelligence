import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WriteConfirmationCard } from '../agent/AgentInteractionCards'
import { mockResponse } from './helpers'

// 简历回填确认卡：模型只能发起 preflight 申请；确认卡展示新旧简历 diff
// （分段新增/更新/无变化 + 字数与摘要），用户点确认 → activate + commit；
// 取消零写请求；终态回填 record-turn。

const activateUrl = '/api/v1/write-confirmations/activate'
const commitUrl = '/api/v1/candidates/resume-backfill/commit'
const recordTurnUrl = '/api/v1/copilot/sessions/record-turn'

const backfillRequest = {
  kind: 'resume_backfill',
  preflight_token: 'tok-rb-1',
  expires_at: '2099-01-01T00:00:00',
  action: 'resume_backfill',
  candidate: { id: 901, name: '杜明', stage: 'S1 新增寻访/待复核', client: '华虹客户', job: '设备工程师' },
  resume: {
    resume_id: 'res-du-1',
    source_url: 'https://h.liepin.com/resume/showresumedetail/?res_id_encode=res-du-1',
    captured_at: '2026-08-19T10:00:00',
    full_text_chars: 1600,
  },
  diff: [
    { field: 'full_text', label: '简历全文', change: 'updated', before_chars: 900, after_chars: 1600, before_excerpt: '旧全文…', after_excerpt: '新全文含 12 吋产线经历…' },
    { field: 'work_text', label: '工作经历', change: 'added', before_chars: 0, after_chars: 300, before_excerpt: '', after_excerpt: '华虹半导体 设备工程师…' },
    { field: 'project_text', label: '项目经历', change: 'unchanged', before_chars: 120, after_chars: 120, before_excerpt: '项目…', after_excerpt: '项目…' },
    { field: 'current_company', label: '当前公司', change: 'kept', before_chars: 5, after_chars: 5, before_excerpt: '华虹半导体', after_excerpt: '华虹半导体' },
  ],
  impact: '简历档案将按当前页快照更新，并记入统一审计。',
  client_request_id: 'agent_req-rb1',
}

describe('简历回填确认卡', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({ ok: true, summary: 'ASA 从已打开的猎聘详情页补全简历：杜明；工作经历 300 字。', already_applied: false })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染待确认卡：人选/阶段/简历来源 + 新旧 diff（新增/更新/无变化/保留原值）', () => {
    render(<WriteConfirmationCard request={backfillRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('简历回填')
    expect(card).toHaveTextContent('杜明')
    expect(card).toHaveTextContent('S1 新增寻访/待复核')
    expect(card).toHaveTextContent('猎聘详情页（档案 res-du-1）')
    expect(card).toHaveTextContent('2026-08-19T10:00:00')
    const diff = card.querySelector('[aria-label="简历新旧比对"]')
    expect(diff).not.toBeNull()
    expect(card).toHaveTextContent('更新：本地 900 字 → 页面 1600 字')
    expect(card).toHaveTextContent('新增：本地 0 字 → 页面 300 字')
    expect(card).toHaveTextContent('无变化（120 字）')
    expect(card).toHaveTextContent('保留原值：华虹半导体')
    expect(card).toHaveTextContent('新全文含 12 吋产线经历…')
    expect(screen.getByRole('button', { name: '确认执行' })).toBeEnabled()
  })

  it('确认：先 activate 后 resume-backfill/commit（同一 token），回执带服务端摘要并回填 confirmed', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={backfillRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('补全简历：杜明')

    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    const activateIndex = urls.findIndex(url => url.includes(activateUrl))
    const commitIndex = urls.findIndex(url => url.includes(commitUrl))
    expect(activateIndex).toBeGreaterThanOrEqual(0)
    expect(commitIndex).toBeGreaterThan(activateIndex)
    expect(JSON.parse(String((fetchMock.mock.calls[commitIndex][1] as RequestInit).body))).toMatchObject({
      candidate_id: 901, preflight_token: 'tok-rb-1',
    })
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(backfill).toBeDefined()
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({
        session_id: 'asa-s1', request_id: 'agent_req-rb1',
        confirm_result: { state: 'confirmed' },
      })
    })
  })

  it('取消：零写请求，展示已取消并回填 cancelled 终态', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={backfillRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(await screen.findByRole('region', { name: '写入确认已取消' })).toHaveTextContent('已取消，未写入')
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes(activateUrl))).toBe(false)
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes(commitUrl))).toBe(false)
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(backfill).toBeDefined()
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({ confirm_result: { state: 'cancelled' } })
    })
  })
})
