import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WriteConfirmationCard } from '../agent/AgentInteractionCards'
import { mockResponse } from './helpers'

// DSH 写确认卡（人确认闸门的 UI 侧）：模型只能发起 preflight 申请；
// 用户点确认 → 前端调 Core activate（UA 门控）+ 写端点；取消零写请求；
// 四态 pending/confirmed/cancelled/drift + expired；终态回填 record-turn。

const activateUrl = '/api/v1/write-confirmations/activate'
const commitUrl = '/api/v1/candidate-actions/commit'
const decisionUrl = '/api/v1/approvals/approval_1/decision'
const recordTurnUrl = '/api/v1/copilot/sessions/record-turn'

const candidateRequest = {
  kind: 'candidate_action',
  preflight_token: 'tok-dsh-1',
  expires_at: '2099-01-01T00:00:00',
  action: 'advance',
  candidate: { id: 558, name: '张桂芳', stage: 'S1 新增寻访/待复核' },
  impact: '候选人关系状态将更新，并写入业务时间线和统一审计。',
  client_request_id: 'agent_req-1',
}

const approvalRequest = {
  kind: 'approval_decision',
  preflight_token: 'tok-dsh-2',
  expires_at: '2099-01-01T00:00:00',
  approval: { approval_id: 'approval_1', workflow_id: 'workflow_1', title: '外部寻访审批', goal_title: '士兰微寻访', decision: 'approve' },
  note: '同意本轮寻访',
  impact: '审批决定将写入工作流状态，并记入统一审计。',
  client_request_id: 'agent_req-2',
}

describe('DSH 写确认卡', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({ ok: true, stage: 'S2 复核通过/待联系' })
      if (url.includes(decisionUrl)) return mockResponse({ ok: true })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染待确认卡：候选人、阶段、影响与确认/取消按钮', () => {
    render(<WriteConfirmationCard request={candidateRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('张桂芳')
    expect(card).toHaveTextContent('S1 新增寻访/待复核')
    expect(card).toHaveTextContent('候选人关系状态将更新')
    expect(screen.getByRole('button', { name: '确认执行' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeEnabled()
  })

  it('确认：先 activate 后 commit（同一 token），显示回执并回填 confirmed 终态', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={candidateRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('张桂芳')

    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    const activateIndex = urls.findIndex(url => url.includes(activateUrl))
    const commitIndex = urls.findIndex(url => url.includes(commitUrl))
    expect(activateIndex).toBeGreaterThanOrEqual(0)
    expect(commitIndex).toBeGreaterThan(activateIndex)
    expect(JSON.parse(String((fetchMock.mock.calls[activateIndex][1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-dsh-1' })
    expect(JSON.parse(String((fetchMock.mock.calls[commitIndex][1] as RequestInit).body))).toMatchObject({
      candidate_id: 558, action: 'advance', preflight_token: 'tok-dsh-1',
    })
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(backfill).toBeDefined()
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({
        session_id: 'asa-s1', request_id: 'agent_req-1',
        confirm_result: { state: 'confirmed' },
      })
    })
  })

  it('防双击：确认中重复点击只提交一次', async () => {
    render(<WriteConfirmationCard request={candidateRequest} sessionId="asa-s1" />)
    const confirm = screen.getByRole('button', { name: '确认执行' })
    fireEvent.click(confirm)
    fireEvent.click(confirm)
    await screen.findByRole('region', { name: '写入执行回执' })
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes(commitUrl))).toHaveLength(1)
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes(activateUrl))).toHaveLength(1)
  })

  it('取消：零写请求，展示已取消并回填 cancelled 终态', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={candidateRequest} sessionId="asa-s1" />)
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

  it('审批决定卡：确认走 activate + decision（携带 preflight_token 与 note）', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={approvalRequest} sessionId="asa-s1" />)
    expect(screen.getByRole('region', { name: '写入确认' })).toHaveTextContent('外部寻访审批')
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('已批准审批')
    const decision = fetchMock.mock.calls.find(([input]) => String(input).includes(decisionUrl))
    expect(decision).toBeDefined()
    expect(JSON.parse(String((decision?.[1] as RequestInit).body))).toMatchObject({
      decision: 'approve', note: '同意本轮寻访', preflight_token: 'tok-dsh-2',
    })
  })

  it('409 漂移：展示服务端中文 detail，确认按钮禁用，不再发写请求', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(activateUrl)) return mockResponse({ detail: '预检令牌无效、已过期或已被使用，请重新发起预检' }, false, 409)
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={candidateRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('预检令牌无效、已过期或已被使用')
    expect(screen.getByRole('button', { name: '确认执行' })).toBeDisabled()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes(commitUrl))).toBe(false)
  })

  it('已过期（expires_at 已过）：展示过期态，不提供确认按钮', () => {
    render(<WriteConfirmationCard request={{ ...candidateRequest, expires_at: '2020-01-01T00:00:00' }} sessionId="asa-s1" />)
    expect(screen.getByRole('region', { name: '写入确认已过期' })).toHaveTextContent('确认请求已过期')
    expect(screen.queryByRole('button', { name: '确认执行' })).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('恢复的终态：confirmed 渲染回执、cancelled 渲染已取消（均不发请求）', () => {
    const { unmount } = render(<WriteConfirmationCard request={{ ...candidateRequest, state: 'confirmed', result_summary: '已确认并同步到 ASA' }} sessionId="asa-s1" />)
    expect(screen.getByRole('region', { name: '写入执行回执' })).toHaveTextContent('已确认并同步到 ASA')
    unmount()
    render(<WriteConfirmationCard request={{ ...candidateRequest, state: 'cancelled' }} sessionId="asa-s1" />)
    expect(screen.getByRole('region', { name: '写入确认已取消' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
