import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { KnowledgeProposalsPanel } from '../panels/KnowledgeProposals'
import type { KnowledgeProposalBrief, KnowledgeProposalDetailPayload, KnowledgeProposalListPayload } from '../api'
import { mockResponse } from './helpers'

// 二期知识飞轮（knowledge_proposal）顾问入口：
// 1) 提案列表 + 状态过滤徽标 + 空态；
// 2) 手动生成：回执如实呈现新建/已存在/留候选统计，证据不足的聚类进候选区块；
// 3) 展开提案：内容 + 可读证据列表；
// 4) 接受两段确认（预检展示影响面 → 确认才提交），回执展示并回读刷新；
// 5) 拒绝必须填原因；决策失败（409）如实报错不误报成功。

const brief: KnowledgeProposalBrief = {
  proposal_id: 'kprop-1',
  proposal_type: 'negative_rule',
  proposal_type_label: '排除规则',
  title: '排除规则建议：聚类客户甲 × 方向不符',
  status: 'pending',
  status_label: '待确认',
  created_at: '2026-08-05 10:00:00',
  decided_at: null,
  decided_by: null,
}

const listPayload = (items = [brief]): KnowledgeProposalListPayload => ({
  ok: true,
  status: 'pending',
  items,
  counts: { pending: items.length, accepted: 0, rejected: 0, superseded: 0 },
  type_labels: { negative_rule: '排除规则' },
})

const detailPayload = (overrides: Partial<KnowledgeProposalDetailPayload> = {}): KnowledgeProposalDetailPayload => ({
  ...brief,
  ok: true,
  content: {
    scope_type: 'client', scope: '聚类客户甲',
    rule: '客户「聚类客户甲」人选多次因「方向不符」停止推进（3 次），建议在寻访策略中把该特征固化为排除/前置过滤规则',
    trigger: 'stop_reason_cluster', trigger_code: 'direction_mismatch', occurrences: 3,
  },
  evidence: [
    {
      source_type: 'stop_reason',
      source_ids: [9100, 9101, 9102],
      summary: '聚类客户甲 岗位停止原因「方向不符」累计 3 次',
      samples: [{ job_candidate_id: 9100, job_title: '刻蚀工程师' }],
    },
  ],
  ...overrides,
})

describe('知识增补提案（knowledge_proposal）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.startsWith('/api/v1/knowledge-proposals?')) return mockResponse(listPayload())
      if (url.endsWith('/api/v1/knowledge-proposals/kprop-1')) return mockResponse(detailPayload())
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('展示提案列表与状态徽标；空态给出可读提示', async () => {
    const first = render(<KnowledgeProposalsPanel />)
    expect(await screen.findByText('排除规则建议：聚类客户甲 × 方向不符')).toBeInTheDocument()
    expect(screen.getByText(/排除规则 ·/)).toBeInTheDocument()
    const pendingTab = screen.getByRole('tab', { name: /待确认/ })
    expect(within(pendingTab).getByText('1')).toBeInTheDocument()
    first.unmount()

    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.startsWith('/api/v1/knowledge-proposals?')) return mockResponse(listPayload([]))
      throw new Error(`未预期的请求：${url}`)
    })
    render(<KnowledgeProposalsPanel />)
    expect(await screen.findByText(/当前状态下暂无提案/)).toBeInTheDocument()
  })

  it('扫描生成提案：回执呈现统计，证据不足的聚类进入候选区块', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/generate')) {
        return mockResponse({
          ok: true,
          created: [brief],
          existing: [],
          candidates: [{ kind: 'stop_reason', key: '稀疏客户乙 × 方向不符', count: 1, needed: 3, reason: '证据不足：1 次 < 阈值 3 次，只留候选' }],
          receipt: { idempotent_replay: false, request_id: 'r1' },
        })
      }
      if (url.startsWith('/api/v1/knowledge-proposals?')) return mockResponse(listPayload())
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<KnowledgeProposalsPanel />)
    await screen.findByText('排除规则建议：聚类客户甲 × 方向不符')

    await user.click(screen.getByRole('button', { name: /扫描生成提案/ }))
    expect(await screen.findByText(/新建提案 1 条，已存在 0 条，证据不足留候选 1 条/)).toBeInTheDocument()
    expect(screen.getByText('稀疏客户乙 × 方向不符')).toBeInTheDocument()
    expect(screen.getByText(/证据不足：1 次 < 阈值 3 次/)).toBeInTheDocument()
    const generateCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/generate'))
    expect((generateCall?.[1] as RequestInit).method).toBe('POST')
  })

  it('展开提案：展示内容与可读证据列表', async () => {
    const user = userEvent.setup()
    render(<KnowledgeProposalsPanel />)
    await user.click(await screen.findByRole('button', { name: '查看提案：排除规则建议：聚类客户甲 × 方向不符' }))

    expect(await screen.findByText(/多次因「方向不符」停止推进（3 次）/)).toBeInTheDocument()
    expect(screen.getByText('停止原因聚类')).toBeInTheDocument()
    expect(screen.getByText('聚类客户甲 岗位停止原因「方向不符」累计 3 次')).toBeInTheDocument()
    expect(screen.getByText(/job_candidate_id：9100 · job_title：刻蚀工程师/)).toBeInTheDocument()
  })

  it('接受两段确认：预检展示影响面，确认后提交并回读刷新', async () => {
    let accepted = false
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/preflight')) {
        return mockResponse({ ok: true, confirmation_token: 'tok-1', expires_in: 300, signature: 'sig-1', impact: '接受后把该规则追加进 kb_agent_confirmed_rules_v1.json。' })
      }
      if (url.endsWith('/decision')) {
        accepted = true
        return mockResponse({
          ok: true, proposal_id: 'kprop-1', decision: 'accept', status: 'accepted', status_label: '已入库',
          applied_to: '/tmp/kb/kb_agent_confirmed_rules_v1.json',
          receipt: { idempotent_replay: false, request_id: 'r2', audit_event_id: 9 },
        })
      }
      if (url.endsWith('/api/v1/knowledge-proposals/kprop-1')) {
        return mockResponse(detailPayload(accepted
          ? { status: 'accepted', status_label: '已入库', applied_to: '/tmp/kb/kb_agent_confirmed_rules_v1.json', decided_at: '2026-08-05 11:00:00' }
          : {}))
      }
      if (url.startsWith('/api/v1/knowledge-proposals?')) {
        if (url.includes('status=accepted')) {
          return mockResponse(listPayload([{ ...brief, status: 'accepted', status_label: '已入库', decided_at: '2026-08-05 11:00:00' }]))
        }
        return mockResponse(listPayload(accepted ? [] : [brief]))
      }
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<KnowledgeProposalsPanel />)
    await user.click(await screen.findByRole('button', { name: '查看提案：排除规则建议：聚类客户甲 × 方向不符' }))
    await screen.findByText('提案内容')

    // 第一段：预检，只展示影响面，不发 decision
    await user.click(screen.getByRole('button', { name: /接受并写入知识库/ }))
    expect(await screen.findByText(/接受后把该规则追加进 kb_agent_confirmed_rules_v1.json/)).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/decision'))).toBe(false)

    // 第二段：确认提交，回执展示并回读
    await user.click(screen.getByRole('button', { name: /确认接受并入库/ }))
    expect(await screen.findByText(/提案已确认（已入库），已写入：\/tmp\/kb\/kb_agent_confirmed_rules_v1.json/)).toBeInTheDocument()
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/decision'))
    const init = decisionCall?.[1] as RequestInit
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toContain('/decision')
    expect(JSON.parse(String(init.body))).toMatchObject({ confirmation_token: 'tok-1', decision: 'accept' })
    // 回读：详情呈现处理结果
    expect(await screen.findByText('处理结果')).toBeInTheDocument()
    expect(screen.getByText(/已入库 · /)).toBeInTheDocument()
  })

  it('拒绝必须填原因；提交后原因留痕', async () => {
    let rejected = false
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/preflight')) return mockResponse({ ok: true, confirmation_token: 'tok-r', expires_in: 300, signature: 'sig-1' })
      if (url.endsWith('/decision')) {
        rejected = true
        return mockResponse({ ok: true, proposal_id: 'kprop-1', decision: 'reject', status: 'rejected', status_label: '已拒绝', receipt: { idempotent_replay: false } })
      }
      if (url.endsWith('/api/v1/knowledge-proposals/kprop-1')) {
        return mockResponse(detailPayload(rejected
          ? { status: 'rejected', status_label: '已拒绝', decision_note: '证据样本太薄', decided_at: '2026-08-05 12:00:00' }
          : {}))
      }
      if (url.startsWith('/api/v1/knowledge-proposals?')) {
        if (url.includes('status=rejected')) {
          return mockResponse(listPayload([{ ...brief, status: 'rejected', status_label: '已拒绝', decided_at: '2026-08-05 12:00:00' }]))
        }
        return mockResponse(listPayload(rejected ? [] : [brief]))
      }
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<KnowledgeProposalsPanel />)
    await user.click(await screen.findByRole('button', { name: '查看提案：排除规则建议：聚类客户甲 × 方向不符' }))
    await screen.findByText('提案内容')

    await user.click(screen.getByRole('button', { name: /拒绝提案/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('请先填写拒绝原因，再提交。')
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/decision'))).toBe(false)

    await user.type(screen.getByLabelText('拒绝原因'), '证据样本太薄')
    await user.click(screen.getByRole('button', { name: /拒绝提案/ }))
    expect(await screen.findByText(/提案已拒绝（已拒绝），原因已留痕/)).toBeInTheDocument()
    expect(await screen.findByText('原因：证据样本太薄')).toBeInTheDocument()
  })

  it('决策失败（409 内容漂移）：如实报错，不误报成功', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/preflight')) return mockResponse({ ok: true, confirmation_token: 'tok-x', expires_in: 300, signature: 'sig-1', impact: '接受后写入知识文件。' })
      if (url.endsWith('/decision')) return mockResponse({ detail: '提案内容已变化，请重新预检' }, false, 409)
      if (url.endsWith('/api/v1/knowledge-proposals/kprop-1')) return mockResponse(detailPayload())
      if (url.startsWith('/api/v1/knowledge-proposals?')) return mockResponse(listPayload())
      throw new Error(`未预期的请求：${url}`)
    })
    const user = userEvent.setup()
    render(<KnowledgeProposalsPanel />)
    await user.click(await screen.findByRole('button', { name: '查看提案：排除规则建议：聚类客户甲 × 方向不符' }))
    await screen.findByText('提案内容')
    await user.click(screen.getByRole('button', { name: /接受并写入知识库/ }))
    await screen.findByText(/接受后写入知识文件/)
    await user.click(screen.getByRole('button', { name: /确认接受并入库/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('提案确认失败：提案内容已变化，请重新预检')
    expect(screen.queryByText(/提案已确认/)).not.toBeInTheDocument()
  })
})
