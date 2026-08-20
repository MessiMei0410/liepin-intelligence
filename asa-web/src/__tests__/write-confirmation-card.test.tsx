import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

  it('合并去重卡：展示双方 diff，确认走 activate + commit（携带 loser_id）', async () => {
    const mergeRequest = {
      kind: 'candidate_action',
      preflight_token: 'tok-dsh-merge',
      expires_at: '2099-01-01T00:00:00',
      action: 'merge',
      candidate: { id: 969, name: '武先生', stage: '触达待核验' },
      merge: {
        winner: { id: 969, name: '武先生' },
        loser: { id: 546, name: '武斌', stage: 'H5 最近寻访/初筛不通过' },
        diff: [
          { field: 'name', label: '姓名', winner: '武先生', loser: '武斌', same: false },
          { field: 'current_company', label: '当前公司', winner: '晶盛机电（半导体、光伏设备）', loser: '晶盛机电', same: false },
          { field: 'current_title', label: '当前职位', winner: '机械工程师', loser: '机械工程师', same: true },
        ],
        loser_already_stopped: false,
      },
      impact: '合并不物理删行：废弃方关系将停止推进（停止原因：重复人选）并备注指向保留方。',
      client_request_id: 'agent_req-merge',
    }
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={mergeRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('合并去重')
    expect(card).toHaveTextContent('武先生（关系 #969）')
    expect(card).toHaveTextContent('武斌（关系 #546）')
    const diff = within(card).getByLabelText('合并字段比对')
    expect(diff).toHaveTextContent('保留：武先生 ｜ 废弃：武斌')
    expect(diff).toHaveTextContent('机械工程师')
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('已合并去重')
    const commit = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(commit).toBeDefined()
    expect(JSON.parse(String((commit?.[1] as RequestInit).body))).toMatchObject({
      candidate_id: 969, action: 'merge', loser_id: 546, preflight_token: 'tok-dsh-merge',
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

// dogfood P2：记面试/生命周期事件确认卡（record_event）——事件要点展示 + commit 携带事件字段。
const recordEventRequest = {
  kind: 'candidate_action',
  preflight_token: 'tok-dsh-re',
  expires_at: '2099-01-01T00:00:00',
  action: 'record_event',
  candidate: { id: 968, name: '陈**', stage: '触达待核验' },
  event: {
    event_type: 'interview_scheduled', label: '面试安排',
    event_status: 'scheduled', occurred_at: '2026-08-20 14:00:00', notes: '一面：客户现场',
  },
  impact: '将在业务时间线记录「面试安排」事件，并自动生成跟进待办（不自动对外发任何消息）。',
  client_request_id: 'agent_req-re',
}

describe('DSH 写确认卡（record_event 记面试）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({
        ok: true, already_recorded: false,
        event: { id: 5001, event_type: 'interview_scheduled', event_type_label: '面试安排', event_status: 'scheduled', event_time: '2026-08-20 14:00:00', summary: '面试安排：一面：客户现场' },
        followup: { id: 77, task_type: 'interview_followup', due_at: '2026-08-22 14:00:00', status: 'open' },
      })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染事件要点：人选/阶段/事件类型/时间/状态', () => {
    render(<WriteConfirmationCard request={recordEventRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('记录面试/事件')
    expect(card).toHaveTextContent('陈**')
    expect(card).toHaveTextContent('触达待核验')
    expect(card).toHaveTextContent('面试安排')
    expect(card).toHaveTextContent('2026-08-20 14:00:00')
    expect(card).toHaveTextContent('跟进待办')
  })

  it('确认：commit 携带 event_type/occurred_at/note，回执展示已记录', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={recordEventRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('面试安排')
    const commit = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(commit).toBeDefined()
    expect(JSON.parse(String((commit?.[1] as RequestInit).body))).toMatchObject({
      candidate_id: 968, action: 'record_event', preflight_token: 'tok-dsh-re',
      event_type: 'interview_scheduled', event_status: 'scheduled', occurred_at: '2026-08-20 14:00:00',
      note: '一面：客户现场',
    })
  })
})

// dogfood R2-3：岗位筛选口径便签确认卡（filter_note）——岗位/新旧便签展示 + commit 携带 note。
const filterNoteRequest = {
  kind: 'filter_note',
  preflight_token: 'tok-dsh-fn',
  expires_at: '2099-01-01T00:00:00',
  action: 'job_filter_note',
  job: { id: 137, title: '机械高级工程师', client: '长越科技' },
  note: '六自由度运动台（6-DOF）经验作为大加分项',
  previous_note: '',
  impact: '确认后保存为该岗位的筛选口径便签：之后出名单卡时随口径声明显示。',
  client_request_id: 'agent_req-fn',
}

describe('DSH 写确认卡（filter_note 口径便签）', () => {
  let fetchMock: Mock<typeof fetch>
  const filterNoteCommitUrl = '/api/v1/jobs/137/filter-notes'

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(filterNoteCommitUrl)) return mockResponse({ ok: true, job_id: 137, note: filterNoteRequest.note, already_saved: false })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染岗位与新旧便签对照', () => {
    render(<WriteConfirmationCard request={filterNoteRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('保存筛选口径便签')
    expect(card).toHaveTextContent('长越科技 / 机械高级工程师')
    expect(card).toHaveTextContent('六自由度运动台（6-DOF）经验作为大加分项')
    expect(card).toHaveTextContent('当前便签')
  })

  it('确认：activate 后 POST jobs/{id}/filter-notes（携带 note 与 token），回执展示已保存', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={filterNoteRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('已保存筛选口径便签')
    const commit = fetchMock.mock.calls.find(([input]) => String(input).includes(filterNoteCommitUrl))
    expect(commit).toBeDefined()
    expect(JSON.parse(String((commit?.[1] as RequestInit).body))).toMatchObject({
      note: '六自由度运动台（6-DOF）经验作为大加分项', preflight_token: 'tok-dsh-fn',
    })
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(backfill).toBeDefined()
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({ confirm_result: { state: 'confirmed' } })
    })
  })
})

// 重新预检（repreflight）：expired（token 5 分钟过期）与 drift（409 漂移失败）
// 的卡不再是死路——用卡片自身参数走对应 preflight 端点换新 token 回到 pending；
// 确认动作仍由人点「确认执行」，重预检不直接执行写入。
const candidatePreflightUrl = '/api/v1/candidate-actions/preflight'
const resumeBackfillPreflightUrl = '/api/v1/candidates/resume-backfill/preflight'

const expiredCandidateRequest = { ...candidateRequest, expires_at: '2020-01-01T00:00:00' }

describe('DSH 写确认卡（重新预检）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(candidatePreflightUrl)) return mockResponse({ token: 'tok-new-1', impact: '候选人将进入复核通过阶段', expires_at: '2099-01-01T00:00:00' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({ ok: true, stage: 'S2 复核通过/待联系' })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('过期卡出「重新预检」按钮（替换死路文案），不提供确认按钮', () => {
    render(<WriteConfirmationCard request={expiredCandidateRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认已过期' })
    expect(card).toHaveTextContent('确认请求已过期')
    expect(card).not.toHaveTextContent('请让 ASA 重新发起')
    expect(screen.getByRole('button', { name: '重新预检' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '确认执行' })).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('重预检成功回到可确认态，确认执行走新 token', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={expiredCandidateRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('region', { name: '写入确认' })).toHaveTextContent('张桂芳')
    const preflight = fetchMock.mock.calls.find(([input]) => String(input).includes(candidatePreflightUrl))
    expect(preflight).toBeDefined()
    expect(JSON.parse(String((preflight?.[1] as RequestInit).body))).toMatchObject({ candidate_id: 558, action: 'advance' })
    // 回到 pending：仍未写入，确认动作由人再点一次
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes(commitUrl))).toBe(false)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('张桂芳')
    const activate = fetchMock.mock.calls.find(([input]) => String(input).includes(activateUrl))
    const commit = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(JSON.parse(String((activate?.[1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-new-1' })
    expect(JSON.parse(String((commit?.[1] as RequestInit).body))).toMatchObject({
      candidate_id: 558, action: 'advance', preflight_token: 'tok-new-1',
    })
  })

  it('重预检失败：展示服务端中文错误，卡保持过期态可再试', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(candidatePreflightUrl)) return mockResponse({ detail: '候选人状态已变化，请重新评估' }, false, 409)
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={expiredCandidateRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('候选人状态已变化')
    expect(screen.getByRole('region', { name: '写入确认已过期' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新预检' })).toBeEnabled()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes(commitUrl))).toBe(false)
  })

  it('漂移 409 卡同样可重预检：换新 token 后确认执行成功', async () => {
    let activateFailures = 1
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(candidatePreflightUrl)) return mockResponse({ token: 'tok-new-2', impact: '候选人将进入复核通过阶段', expires_at: '2099-01-01T00:00:00' })
      if (url.includes(activateUrl)) {
        if (activateFailures > 0) {
          activateFailures -= 1
          return mockResponse({ detail: '预检令牌无效、已过期或已被使用，请重新发起预检' }, false, 409)
        }
        return mockResponse({ ok: true, activated: true })
      }
      if (url.includes(commitUrl)) return mockResponse({ ok: true, stage: 'S2 复核通过/待联系' })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={candidateRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('预检令牌无效、已过期或已被使用')
    expect(screen.getByRole('button', { name: '确认执行' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: '确认执行' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('张桂芳')
    const commit = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(JSON.parse(String((commit?.[1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-new-2' })
  })

  it('record_event 过期卡重预检：携带事件字段（token 绑定事件类型）', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={{ ...recordEventRequest, expires_at: '2020-01-01T00:00:00' }} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('region', { name: '写入确认' })).toHaveTextContent('记录面试/事件')
    const preflight = fetchMock.mock.calls.find(([input]) => String(input).includes(candidatePreflightUrl))
    expect(JSON.parse(String((preflight?.[1] as RequestInit).body))).toMatchObject({
      candidate_id: 968, action: 'record_event',
      event_type: 'interview_scheduled', event_status: 'scheduled', occurred_at: '2026-08-20 14:00:00',
      note: '一面：客户现场',
    })
  })

  it('merge 过期卡重预检：携带 loser_id', async () => {
    const mergeExpired = {
      kind: 'candidate_action',
      preflight_token: 'tok-dsh-merge',
      expires_at: '2020-01-01T00:00:00',
      action: 'merge',
      candidate: { id: 969, name: '武先生', stage: '触达待核验' },
      merge: {
        winner: { id: 969, name: '武先生' },
        loser: { id: 546, name: '武斌', stage: 'H5 最近寻访/初筛不通过' },
        diff: [], loser_already_stopped: false,
      },
      impact: '合并不物理删行。',
      client_request_id: 'agent_req-merge-exp',
    }
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={mergeExpired} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('region', { name: '写入确认' })).toHaveTextContent('合并去重')
    const preflight = fetchMock.mock.calls.find(([input]) => String(input).includes(candidatePreflightUrl))
    expect(JSON.parse(String((preflight?.[1] as RequestInit).body))).toMatchObject({
      candidate_id: 969, action: 'merge', loser_id: 546,
    })
  })

  it('简历回填重预检：快照过期（30 分钟 TTL）给出明确提示而非裸错', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(resumeBackfillPreflightUrl)) return mockResponse({ detail: '未读到当前页简历快照：请先在猎聘打开该人选的详情页（浏览器扩展会自动上报快照），再发起简历回填' }, false, 409)
      throw new Error(`未预期的请求：${url}`)
    })
    const backfillExpired = {
      kind: 'resume_backfill',
      preflight_token: 'tok-rb-exp',
      expires_at: '2020-01-01T00:00:00',
      action: 'resume_backfill',
      candidate: { id: 901, name: '杜明', stage: 'S1 新增寻访/待复核', client: '华虹客户', job: '设备工程师' },
      resume: { resume_id: 'res-du-1', captured_at: '2026-08-19T10:00:00' },
      diff: [{ field: 'full_text', label: '简历全文', change: 'updated', before_chars: 900, after_chars: 1600 }],
      impact: '简历档案将按当前页快照更新。',
      client_request_id: 'agent_req-rb-exp',
    }
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={backfillExpired} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('页面快照已过期，请在详情页重新打开后再试')
    const preflight = fetchMock.mock.calls.find(([input]) => String(input).includes(resumeBackfillPreflightUrl))
    expect(JSON.parse(String((preflight?.[1] as RequestInit).body))).toMatchObject({ candidate_id: 901, resume_id: 'res-du-1' })
  })
})

// 岗位建档确认卡（job_create）：客户解析结果（含「将新建客户」）/岗位/方向/Base/JD +
// 提示行展示；确认走 activate + POST /api/v1/jobs；过期/409 可重预检换新 token。
const jobCreateRequest = {
  kind: 'job_create',
  preflight_token: 'tok-dsh-jc',
  expires_at: '2099-01-01T00:00:00',
  action: 'job_create',
  job: {
    client: '杭州士兰微电子有限公司', client_id: 2, client_is_new: false, client_match: 'fuzzy',
    title: '市场总监', direction: '汽车市场', base: '杭州', priority: '', jd_text: '',
  },
  warnings: ['客户名「士兰微」按既有客户「杭州士兰微电子有限公司」匹配建档', '未提供 JD 文本：建档后岗位职责/要求为空，可后续补充'],
  impact: '确认后将在既有客户「杭州士兰微电子有限公司」下建档岗位「市场总监」（初始状态：待启动）；建档只登记岗位，不会自动启动任何寻访/抓取。',
  client_request_id: 'agent_req-jc',
}
const jobCreatePreflightUrl = '/api/v1/jobs/preflight'
const jobCreateCommitUrl = '/api/v1/jobs'

describe('DSH 写确认卡（job_create 岗位建档）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(jobCreatePreflightUrl)) return mockResponse({ token: 'tok-jc-new', expires_at: '2099-01-01T00:00:00' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(jobCreateCommitUrl)) return mockResponse({
        ok: true, already_created: false, job_id: 401, client_id: 2,
        client_name: '杭州士兰微电子有限公司', client_created: false, title: '市场总监',
      })
      if (url.includes(recordTurnUrl)) return mockResponse({ ok: true, updated: true })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染客户/岗位/方向/Base/JD 与提示行', () => {
    render(<WriteConfirmationCard request={jobCreateRequest} sessionId="asa-s1" />)
    const card = screen.getByRole('region', { name: '写入确认' })
    expect(card).toHaveTextContent('岗位建档')
    expect(card).toHaveTextContent('杭州士兰微电子有限公司')
    expect(card).toHaveTextContent('市场总监')
    expect(card).toHaveTextContent('汽车市场')
    expect(card).toHaveTextContent('杭州')
    expect(card).toHaveTextContent('未提供（建档后待补充）')
    expect(card).toHaveTextContent('按既有客户「杭州士兰微电子有限公司」匹配建档')
    expect(card).not.toHaveTextContent('将新建客户')
  })

  it('新客户：客户行明示「将新建客户」', () => {
    const newClientRequest = {
      ...jobCreateRequest,
      job: { ...jobCreateRequest.job, client: '全新客户', client_id: null, client_is_new: true, client_match: 'new' },
    }
    render(<WriteConfirmationCard request={newClientRequest} sessionId="asa-s1" />)
    expect(screen.getByRole('region', { name: '写入确认' })).toHaveTextContent('全新客户（将新建客户）')
  })

  it('确认：activate 后 POST /api/v1/jobs（携带字段与 token），回执展示已建档 #id 并回填 confirmed', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={jobCreateRequest} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('已建档：岗位 #401')
    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    const activateIndex = urls.findIndex(url => url.includes(activateUrl))
    const commitIndex = urls.findIndex(url => url.includes(jobCreateCommitUrl) && !url.includes(jobCreatePreflightUrl))
    expect(activateIndex).toBeGreaterThanOrEqual(0)
    expect(commitIndex).toBeGreaterThan(activateIndex)
    expect(JSON.parse(String((fetchMock.mock.calls[commitIndex][1] as RequestInit).body))).toMatchObject({
      client_name: '杭州士兰微电子有限公司', title: '市场总监', direction: '汽车市场', base: '杭州',
      preflight_token: 'tok-dsh-jc',
    })
    await waitFor(() => {
      const backfill = fetchMock.mock.calls.find(([input]) => String(input).includes(recordTurnUrl))
      expect(backfill).toBeDefined()
      expect(JSON.parse(String((backfill?.[1] as RequestInit).body))).toMatchObject({
        session_id: 'asa-s1', request_id: 'agent_req-jc',
        confirm_result: { state: 'confirmed' },
      })
    })
  })

  it('过期卡重预检：POST /api/v1/jobs/preflight 换新 token 后确认执行成功', async () => {
    const user = userEvent.setup()
    render(<WriteConfirmationCard request={{ ...jobCreateRequest, expires_at: '2020-01-01T00:00:00' }} sessionId="asa-s1" />)
    await user.click(screen.getByRole('button', { name: '重新预检' }))
    expect(await screen.findByRole('region', { name: '写入确认' })).toHaveTextContent('岗位建档')
    const preflight = fetchMock.mock.calls.find(([input]) => String(input).includes(jobCreatePreflightUrl))
    expect(preflight).toBeDefined()
    expect(JSON.parse(String((preflight?.[1] as RequestInit).body))).toMatchObject({
      client_name: '杭州士兰微电子有限公司', title: '市场总监',
    })
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(await screen.findByRole('region', { name: '写入执行回执' })).toHaveTextContent('已建档：岗位 #401')
    const activate = fetchMock.mock.calls.find(([input]) => String(input).includes(activateUrl))
    expect(JSON.parse(String((activate?.[1] as RequestInit).body))).toMatchObject({ preflight_token: 'tok-jc-new' })
  })
})
