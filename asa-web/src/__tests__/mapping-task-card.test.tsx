import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MappingTaskCard } from '../workflows/MappingTaskCard'
import { StrategyReviewExpansion } from '../workflows/StrategyReviewExpansion'
import { mockResponse } from './helpers'

// S5-2 Mapping 任务卡视图 + 决策树入口：团队树/人选卡/七态动作/确认破冰/备注 PATCH/入库/失败记账人话/淘汰折叠。
// fetch 全 mock（禁 any），按 URL 路由到各端点响应。

const ICEBREAKER = {
  hooks: ['看到您发的《Analytical loss model of power MOSFET》，这个方向我们客户这边正好在重点投入，想跟您请教两句'],
  angle: '技术共鸣',
  generated_at: '2026-07-23 10:05:00',
  source_ref: 'https://doi.org/10.1109/example（论文《Analytical loss model of power MOSFET》）',
}

const taskPayload = {
  ok: true,
  artifact_id: 'mapping_task_wf-1',
  job_id: 154,
  workflow_id: 'wf-1',
  title: 'Mapping 直挖任务卡：士兰微 技术市场经理/总监（PC电源） v1',
  content: '# 任务卡',
  created_at: '2026-07-23 10:00:00',
  mapping_task: {
    schema_version: 'mapping_v1',
    trigger: 'manual',
    job_id: 154,
    strategy_ref: 'artifact_strategy_1',
    client: '士兰微',
    job_title: '技术市场经理/总监（PC电源）',
    generated_at: '2026-07-23 10:00:00',
    target_teams: [
      { company: 'MPS', team: 'PC 方向 TME 团队', location: '上海', tier: 'T1', confidence: 'high', evidence: [{ type: '官网', ref: 'https://www.monolithicpower.com', as_of: '2026-07-23' }], notes: [] },
      { company: 'MPS', team: 'AE 支持团队', location: '', tier: 'T1', confidence: 'medium', evidence: [], notes: [] },
      { company: '矽力杰', team: '电源 IC 团队', location: '杭州', tier: 'T2', confidence: 'low', evidence: [], notes: [] },
    ],
    candidates: [
      { name: 'Y**', current_role: 'MPS 技术论文作者', team_ref: 0, source_urls: ['https://doi.org/10.1109/example'], confidence: 'medium', reason: 'MPS 相关公开论文《Analytical loss model of power MOSFET》作者（单位标注：Monolithic Power Systems）', status: 'pending', consultant_note: '' },
      { name: 'K**', current_role: 'MPS 应用工程师', team_ref: 1, source_urls: ['https://example.com/k'], confidence: 'high', reason: 'MPS 官网公开联系人', status: 'confirmed', consultant_note: '重点跟', icebreaker: ICEBREAKER },
      { name: 'L**', current_role: '矽力杰 FAE', team_ref: 2, source_urls: ['https://example.com/l'], confidence: 'low', reason: '矽力杰 论文作者', status: 'contacted', consultant_note: '' },
      { name: 'D**', current_role: '矽力杰 工程师', team_ref: 2, source_urls: ['https://example.com/d'], confidence: 'low', reason: '矽力杰 论文作者', status: 'replied', consultant_note: '' },
      { name: 'P**', current_role: '矽力杰 工程师', team_ref: 2, source_urls: ['https://example.com/p'], confidence: 'low', reason: '矽力杰 论文作者', status: 'parked', consultant_note: '' },
      { name: 'I**', current_role: 'MPS 工程师', team_ref: 0, source_urls: ['https://example.com/i'], confidence: 'medium', reason: 'MPS 论文作者', status: 'intaken', consultant_note: '', intake: { job_candidate_id: 559, candidate_id: 88, person_id: 66, intaken_at: '2026-07-23 11:00:00', relation_existed: true } },
      { name: 'R**', current_role: 'MPS 工程师', team_ref: 0, source_urls: ['https://example.com/r'], confidence: 'low', reason: 'MPS 论文作者', status: 'rejected', consultant_note: '' },
    ],
    stats: {
      teams: 3, candidates: 7, confirmed: 3, intaken: 1, clues: 9,
      banned_filtered: 1, rejected_no_source: 0, failures_count: 2,
      failures: [
        { source: '官网', url: 'https://www.silergy.com', reason: 'blocked', note: '' },
        { source: '专利', url: 'https://patents.example.com/x', reason: 'timeout', note: '' },
      ],
      sources: { 官网: 2, 论文: 5 },
    },
  },
}

// 按 URL 路由的 fetch mock：默认 GET 任务卡；handler 可覆盖任意端点响应。
const stubFetch = (handler?: (url: string, init?: RequestInit) => unknown) => {
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const body = handler ? handler(url, init) : undefined
    return mockResponse(body === undefined ? taskPayload : body)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const renderCard = async (openCandidate = vi.fn()) => {
  render(<MappingTaskCard jobId={154} artifactId="mapping_task_wf-1" openCandidate={openCandidate} onClose={() => undefined} />)
  const section = await screen.findByRole('region', { name: 'Mapping 任务卡' })
  await within(section).findByText('目标团队')
  return { section, openCandidate }
}

const cardOf = (name: string) => {
  const element = screen.getByText(name).closest('.mapping-candidate')
  if (!element) throw new Error(`未找到人选卡：${name}`)
  return within(element as HTMLElement)
}

const patchCalls = (fetchMock: ReturnType<typeof stubFetch>) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Mapping 任务卡视图（MappingTaskCard）', () => {
  it('左侧团队树按公司分组渲染团队、地点与把握（大/一般/偏小）', async () => {
    stubFetch()
    const { section } = await renderCard()
    const teams = within(section).getByRole('region', { name: '目标团队' })
    expect(within(teams).getByText('3 个团队 · 2 家公司')).toBeInTheDocument()
    expect(within(teams).getByText('MPS')).toBeInTheDocument()
    expect(within(teams).getByText('矽力杰')).toBeInTheDocument()
    expect(within(teams).getByText('PC 方向 TME 团队')).toBeInTheDocument()
    expect(within(teams).getByText('AE 支持团队')).toBeInTheDocument()
    expect(within(teams).getByText(/上海 · 把握大/)).toBeInTheDocument()
    expect(within(teams).getByText('把握一般')).toBeInTheDocument()
    expect(within(teams).getByText(/杭州 · 把握偏小/)).toBeInTheDocument()
  })

  it('右侧人选卡渲染职务/推荐理由/来源链接（可点新开）/七态中文标签', async () => {
    stubFetch()
    await renderCard()
    const card = cardOf('Y**')
    expect(card.getByText('MPS 技术论文作者')).toBeInTheDocument()
    expect(card.getByText(/Analytical loss model of power MOSFET/)).toBeInTheDocument()
    expect(card.getByText('待确认')).toBeInTheDocument()
    const link = card.getByRole('link', { name: '来源 1' })
    expect(link).toHaveAttribute('href', 'https://doi.org/10.1109/example')
    expect(link).toHaveAttribute('target', '_blank')
    expect(cardOf('L**').getByText('已接触')).toBeInTheDocument()
    expect(cardOf('D**').getByText('已回复')).toBeInTheDocument()
    expect(cardOf('P**').getByText('已搁置')).toBeInTheDocument()
  })

  it('「这份名单的效果」：线索有效率与失败记账人话', async () => {
    stubFetch()
    const { section } = await renderCard()
    const stats = within(section).getByRole('region', { name: '这份名单的效果' })
    expect(within(stats).getByText(/目标团队 3 个 · 名单 7 人 · 禁挖过滤 1/)).toBeInTheDocument()
    // 线索有效率 = 已确认 3 / 采集线索 9 = 33%
    expect(within(stats).getByText('33%')).toBeInTheDocument()
    expect(within(stats).getByText('官网有反爬保护，没抓到')).toBeInTheDocument()
    expect(within(stats).getByText('专利访问超时，没抓到')).toBeInTheDocument()
  })

  it('确认后人选卡显示开场白要点（口播句 + angle + 只读不发送提示）', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (init?.method === 'PATCH') {
        return {
          ok: true, artifact_id: 'mapping_task_wf-1', index: 0,
          candidate: { ...taskPayload.mapping_task.candidates[0], status: 'confirmed', icebreaker: ICEBREAKER },
          status: 'confirmed', status_label: '已确认',
          stats: { ...taskPayload.mapping_task.stats, confirmed: 4 },
          icebreaker_generated: true, icebreaker_errors: [],
        }
      }
      return undefined
    })
    await renderCard()
    const user = userEvent.setup()
    await user.click(cardOf('Y**').getByRole('button', { name: '确认' }))
    // PATCH 打对端点：幂等头 + status=confirmed + request_id
    await waitFor(() => expect(patchCalls(fetchMock)).toHaveLength(1))
    const [url, init] = patchCalls(fetchMock)[0]
    expect(url).toBe('/api/v1/mapping-tasks/mapping_task_wf-1/candidates/0')
    expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^web_/)
    const body = JSON.parse(String(init?.body)) as { status?: string; request_id?: string }
    expect(body.status).toBe('confirmed')
    expect(body.request_id).toMatch(/^web_/)
    // 破冰素材出现且含真实线索词；红线提示在
    const card = cardOf('Y**')
    expect(await card.findByText('开场白要点')).toBeInTheDocument()
    expect(card.getByText(/看到您发的《Analytical loss model of power MOSFET》/)).toBeInTheDocument()
    expect(card.getByText('技术共鸣')).toBeInTheDocument()
    expect(card.getByText(/只读不发送/)).toBeInTheDocument()
    expect(card.getByText('已确认')).toBeInTheDocument()
  })

  it('破冰素材质量不合格时显示原因，不显示素材', async () => {
    stubFetch((url, init) => {
      if (init?.method === 'PATCH') {
        return {
          ok: true, artifact_id: 'mapping_task_wf-1', index: 0,
          candidate: { ...taskPayload.mapping_task.candidates[0], status: 'confirmed' },
          status: 'confirmed', status_label: '已确认',
          stats: taskPayload.mapping_task.stats,
          icebreaker_generated: false,
          icebreaker_errors: ['该候选没有可引用的真实线索（论文题/单位/团队/职务关键词缺失），无法生成开场白要点'],
        }
      }
      return undefined
    })
    await renderCard()
    const user = userEvent.setup()
    await user.click(cardOf('Y**').getByRole('button', { name: '确认' }))
    const card = cardOf('Y**')
    expect(await card.findByText(/开场白要点没生成：该候选没有可引用的真实线索/)).toBeInTheDocument()
    expect(card.queryByText('开场白要点', { exact: true })).not.toBeInTheDocument()
  })

  it('状态迁移按钮按态显隐：pending/confirmed/contacted/replied/parked', async () => {
    stubFetch()
    await renderCard()
    const pending = cardOf('Y**')
    expect(pending.getByRole('button', { name: '确认' })).toBeInTheDocument()
    expect(pending.getByRole('button', { name: '删除' })).toBeInTheDocument()
    expect(pending.queryByRole('button', { name: '入库' })).not.toBeInTheDocument()
    const confirmed = cardOf('K**')
    for (const label of ['已接触', '搁置', '淘汰', '入库', '重新生成开场白']) {
      expect(confirmed.getByRole('button', { name: label })).toBeInTheDocument()
    }
    const contacted = cardOf('L**')
    expect(contacted.getByRole('button', { name: '已回复' })).toBeInTheDocument()
    expect(contacted.queryByRole('button', { name: '入库' })).not.toBeInTheDocument()
    const replied = cardOf('D**')
    for (const label of ['确认', '已接触', '已回复', '入库', '搁置', '淘汰']) {
      expect(replied.queryByRole('button', { name: label })).not.toBeInTheDocument()
    }
    const parked = cardOf('P**')
    expect(parked.getByRole('button', { name: '恢复待确认' })).toBeInTheDocument()
    expect(parked.getByRole('button', { name: '淘汰' })).toBeInTheDocument()
  })

  it('consultant_note 失焦自动保存（PATCH 只带备注）', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as { consultant_note?: string }
        return {
          ok: true, artifact_id: 'mapping_task_wf-1', index: 1,
          candidate: { ...taskPayload.mapping_task.candidates[1], consultant_note: body.consultant_note },
          status: 'confirmed', status_label: '已确认', stats: taskPayload.mapping_task.stats,
          icebreaker_generated: false, icebreaker_errors: [],
        }
      }
      return undefined
    })
    await renderCard()
    const user = userEvent.setup()
    const input = cardOf('K**').getByRole('textbox', { name: 'K** 的顾问备注' })
    await user.clear(input)
    await user.type(input, '客户是平移优先')
    await user.tab()
    await waitFor(() => expect(patchCalls(fetchMock)).toHaveLength(1))
    const [url, init] = patchCalls(fetchMock)[0]
    expect(url).toBe('/api/v1/mapping-tasks/mapping_task_wf-1/candidates/1')
    const body = JSON.parse(String(init?.body)) as { consultant_note?: string; status?: string }
    expect(body.consultant_note).toBe('客户是平移优先')
    expect(body.status).toBeUndefined()
    // 备注未变化时不再发 PATCH
    await user.click(input)
    await user.tab()
    expect(patchCalls(fetchMock)).toHaveLength(1)
  })

  it('入库动作调对端点并显示「已入库」回执，可跳候选人详情', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith('/candidates/1/intake') && init?.method === 'POST') {
        return {
          ok: true, artifact_id: 'mapping_task_wf-1', index: 1, status: 'intaken',
          already_intaken: false, relation_existed: false,
          job_candidate_id: 887, candidate_id: 88, person_id: 66, intaken_at: '2026-07-23 12:00:00',
          stats: { ...taskPayload.mapping_task.stats, intaken: 2 },
        }
      }
      return undefined
    })
    const { openCandidate } = await renderCard()
    const user = userEvent.setup()
    await user.click(cardOf('K**').getByRole('button', { name: '入库' }))
    const card = cardOf('K**')
    expect(await card.findByText(/已入库 · 2026-07-23 12:00/)).toBeInTheDocument()
    expect(card.getByText('已入库')).toBeInTheDocument()
    const intakeCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/candidates/1/intake'))
    expect(intakeCalls).toHaveLength(1)
    expect((intakeCalls[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^web_/)
    await user.click(card.getByRole('button', { name: '查看候选人' }))
    expect(openCandidate).toHaveBeenCalledWith(887)
    // 已入库人选不再出现动作按钮
    expect(card.queryByRole('button', { name: '已接触' })).not.toBeInTheDocument()
  })

  it('已入库回执：关系已存在时提示复用原条目', async () => {
    stubFetch()
    await renderCard()
    const card = cardOf('I**')
    expect(card.getByText(/已入库 · 2026-07-23 11:00/)).toBeInTheDocument()
    expect(card.getByText(/复用原条目，未重复建档/)).toBeInTheDocument()
  })

  it('已淘汰人选折叠到底部（软删可见，默认收起）', async () => {
    stubFetch()
    await renderCard()
    expect(screen.getByText(/已淘汰（1）· 软删保留，不物理删除/)).toBeInTheDocument()
    const rejectedName = screen.getByText('R**')
    expect(rejectedName).not.toBeVisible()
    // 展开后可见且无动作按钮
    const user = userEvent.setup()
    await user.click(screen.getByText(/已淘汰（1）/))
    expect(rejectedName).toBeVisible()
    expect(cardOf('R**').queryByRole('button', { name: '确认' })).not.toBeInTheDocument()
  })

  it('409 业务冲突中文原因直接透出在人选卡上', async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      if (init?.method === 'PATCH') return mockResponse({ detail: '非法状态迁移：replied → confirmed（允许：parked/rejected）' }, false, 409)
      return mockResponse(taskPayload)
    })
    vi.stubGlobal('fetch', fetchMock)
    await renderCard()
    const user = userEvent.setup()
    await user.click(cardOf('Y**').getByRole('button', { name: '确认' }))
    expect(await cardOf('Y**').findByText(/非法状态迁移/)).toBeInTheDocument()
  })
})

describe('扩池决策树 Mapping 入口（StrategyReviewExpansion）', () => {
  const escalateStep = {
    step_id: 'exp-5',
    order: 5,
    action_type: 'escalate_mapping' as const,
    title: '转 Mapping 直挖 / 与客户校准方向（升级项）',
    detail: '本地池与渠道池均已尽，建议转 Mapping 直挖。',
    params: { actions: ['mapping_direct_sourcing', 'client_direction_calibration'], reason: '排重率 91%' },
    status: 'pending',
  }

  it('escalate_mapping 步旁显示「发起 Mapping 直挖」，点击调创建接口并打开任务卡', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url === '/api/v1/jobs/154/mapping-tasks' && init?.method === 'POST') {
        return { ok: true, job_id: 154, workflow_id: 'wf-1', artifact_id: 'mapping_task_wf-1' }
      }
      return undefined
    })
    const onOpenMapping = vi.fn()
    render(<StrategyReviewExpansion workflowId="wf-1" tree={[escalateStep]} jobId={154} onOpenMapping={onOpenMapping} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '发起 Mapping 直挖' }))
    await waitFor(() => expect(onOpenMapping).toHaveBeenCalledWith('mapping_task_wf-1'))
    const createCalls = fetchMock.mock.calls.filter(([url, init]) => String(url) === '/api/v1/jobs/154/mapping-tasks' && init?.method === 'POST')
    expect(createCalls).toHaveLength(1)
    const body = JSON.parse(String(createCalls[0][1]?.body)) as { trigger?: string; request_id?: string }
    expect(body.trigger).toBe('decision_tree_exhausted')
    expect(body.request_id).toMatch(/^web_/)
  })

  it('已有任务卡时按钮变为「打开 Mapping 任务卡」，直接打开不再创建', async () => {
    const fetchMock = stubFetch()
    const onOpenMapping = vi.fn()
    render(<StrategyReviewExpansion workflowId="wf-1" tree={[escalateStep]} jobId={154} mappingArtifactId="mapping_task_wf-1" onOpenMapping={onOpenMapping} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '打开 Mapping 任务卡' }))
    expect(onOpenMapping).toHaveBeenCalledWith('mapping_task_wf-1')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('工作流无 job 上下文或非 escalate_mapping 步时按钮不显示', () => {
    const { unmount } = render(<StrategyReviewExpansion workflowId="wf-1" tree={[escalateStep]} onOpenMapping={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Mapping 直挖|Mapping 任务卡/ })).not.toBeInTheDocument()
    unmount()
    render(<StrategyReviewExpansion workflowId="wf-1" tree={[{ ...escalateStep, step_id: 'exp-1', action_type: 'expand_pool' }]} jobId={154} onOpenMapping={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Mapping 直挖|Mapping 任务卡/ })).not.toBeInTheDocument()
  })
})
