import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MappingTaskCard } from '../workflows/MappingTaskCard'
import { StrategyReviewExpansion } from '../workflows/StrategyReviewExpansion'
import {
  groupMappingTeams,
  humanizeMappingFailure,
  mappingClueRate,
  mappingConfidenceLabel,
  mappingStatusLabel,
} from '../workflows/mappingTask'
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

const renderCard = async (openCandidate = vi.fn(), onChanged?: () => void | Promise<void>) => {
  render(<MappingTaskCard jobId={154} artifactId="mapping_task_wf-1" openCandidate={openCandidate} onClose={() => undefined} onChanged={onChanged} />)
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
    expect(link).toHaveAttribute('title', 'https://doi.org/10.1109/example')
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
    const onChanged = vi.fn()
    const candidateUpdated = vi.fn()
    window.addEventListener('asa:candidate-updated', candidateUpdated, { once: true })
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
    const { openCandidate } = await renderCard(vi.fn(), onChanged)
    const user = userEvent.setup()
    await user.click(cardOf('K**').getByRole('button', { name: '入库' }))
    const card = cardOf('K**')
    expect(await card.findByText(/已入库 · 2026-07-23 12:00/)).toBeInTheDocument()
    expect(card.getByText('已入库')).toBeInTheDocument()
    expect(await card.findByText('入库完成')).toHaveAttribute('role', 'status')
    const intakeCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/candidates/1/intake'))
    expect(intakeCalls).toHaveLength(1)
    expect((intakeCalls[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^web_/)
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(candidateUpdated).toHaveBeenCalledTimes(1)
    expect((candidateUpdated.mock.calls[0][0] as CustomEvent).detail).toEqual({
      id: 887,
      created: true,
      jobId: 154,
      source: 'mapping',
    })
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
    const error = await cardOf('Y**').findByText(/非法状态迁移/)
    expect(error).toBeInTheDocument()
    expect(error).toHaveAttribute('role', 'alert')
  })

  it('加载中给 status 提示；失败给 alert 与重试，重试成功后恢复内容', async () => {
    let resolveLoad!: (response: Response) => void
    const pending = new Promise<Response>(resolve => { resolveLoad = resolve })
    const fetchMock = vi.fn<typeof fetch>()
    fetchMock.mockImplementationOnce(() => pending)
    fetchMock.mockImplementationOnce(() => Promise.resolve(mockResponse(taskPayload)))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<MappingTaskCard jobId={154} artifactId="mapping_task_wf-1" openCandidate={vi.fn()} onClose={() => undefined} />)
    expect(screen.getByRole('status')).toHaveTextContent('任务卡加载中')
    resolveLoad(mockResponse({ detail: '任务卡加载失败（模拟）' }, false, 500))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('任务卡加载失败（模拟）')
    await user.click(screen.getByRole('button', { name: /重新加载/ }))
    await screen.findByText('目标团队')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('动作进行中卡片 aria-busy，按钮禁用，连点不重复发 PATCH', async () => {
    let resolvePatch!: (response: Response) => void
    const patchGate = new Promise<Response>(resolve => { resolvePatch = resolve })
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      if (init?.method === 'PATCH') return patchGate
      return Promise.resolve(mockResponse(taskPayload))
    })
    vi.stubGlobal('fetch', fetchMock)
    await renderCard()
    const user = userEvent.setup()
    await user.click(cardOf('Y**').getByRole('button', { name: '确认' }))
    const cardElement = cardOf('Y**').getByRole('button', { name: '确认' }).closest('.mapping-candidate') as HTMLElement
    expect(cardElement).toHaveAttribute('aria-busy', 'true')
    expect(cardOf('Y**').getByRole('button', { name: '确认' })).toBeDisabled()
    expect(cardOf('Y**').getByRole('button', { name: '删除' })).toBeDisabled()
    // 未返回前重复点（React 对 disabled 按钮不发 click），只允许 1 个 PATCH
    fireEvent.click(cardOf('Y**').getByRole('button', { name: '确认' }))
    fireEvent.click(cardOf('Y**').getByRole('button', { name: '删除' }))
    expect(patchCalls(fetchMock)).toHaveLength(1)
    resolvePatch(mockResponse({
      ok: true, artifact_id: 'mapping_task_wf-1', index: 0,
      candidate: { ...taskPayload.mapping_task.candidates[0], status: 'confirmed' },
      status: 'confirmed', status_label: '已确认', stats: taskPayload.mapping_task.stats,
      icebreaker_generated: false, icebreaker_errors: [],
    }))
    expect(await cardOf('Y**').findByText('已确认')).toBeInTheDocument()
    expect(cardElement).toHaveAttribute('aria-busy', 'false')
  })

  it('不同卡片并发动作互不误清 busy（先完成的不解锁另一张在途卡）', async () => {
    let resolveY!: (response: Response) => void
    const yGate = new Promise<Response>(resolve => { resolveY = resolve })
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      if (String(input).endsWith('/candidates/0') && init?.method === 'PATCH') return yGate
      if (init?.method === 'PATCH') {
        return Promise.resolve(mockResponse({
          ok: true, artifact_id: 'mapping_task_wf-1', index: 2,
          candidate: { ...taskPayload.mapping_task.candidates[2], status: 'replied' },
          status: 'replied', status_label: '已回复', stats: taskPayload.mapping_task.stats,
          icebreaker_generated: false, icebreaker_errors: [],
        }))
      }
      return Promise.resolve(mockResponse(taskPayload))
    })
    vi.stubGlobal('fetch', fetchMock)
    await renderCard()
    const user = userEvent.setup()
    const yCard = cardOf('Y**')
    const lCard = cardOf('L**')
    const yCardElement = yCard.getByRole('button', { name: '确认' }).closest('.mapping-candidate') as HTMLElement
    await user.click(yCard.getByRole('button', { name: '确认' }))
    await user.click(lCard.getByRole('button', { name: '已回复' }))
    // L 已返回，Y 仍在途：Y 的 aria-busy 与禁用状态不能被 L 完成误清
    await waitFor(() => expect(lCard.getByText('已回复')).toBeInTheDocument())
    expect(yCard.getByRole('button', { name: '确认' })).toBeDisabled()
    expect(yCardElement).toHaveAttribute('aria-busy', 'true')
    resolveY(mockResponse({
      ok: true, artifact_id: 'mapping_task_wf-1', index: 0,
      candidate: { ...taskPayload.mapping_task.candidates[0], status: 'confirmed' },
      status: 'confirmed', status_label: '已确认', stats: taskPayload.mapping_task.stats,
      icebreaker_generated: false, icebreaker_errors: [],
    }))
    expect(await yCard.findByText('已确认')).toBeInTheDocument()
    expect(yCardElement).toHaveAttribute('aria-busy', 'false')
  })

  it('状态迁移/备注保存成功给出可访问回执（role=status）', async () => {
    stubFetch((url, init) => {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as { consultant_note?: string }
        return {
          ok: true, artifact_id: 'mapping_task_wf-1', index: 0,
          candidate: {
            ...taskPayload.mapping_task.candidates[0],
            status: 'confirmed',
            consultant_note: body.consultant_note || '',
          },
          status: 'confirmed', status_label: '已确认', stats: taskPayload.mapping_task.stats,
          icebreaker_generated: false, icebreaker_errors: [],
        }
      }
      return undefined
    })
    await renderCard()
    const user = userEvent.setup()
    const card = cardOf('Y**')
    await user.click(card.getByRole('button', { name: '确认' }))
    expect(await card.findByText('状态已更新为已确认')).toHaveAttribute('role', 'status')
    const note = card.getByRole('textbox', { name: 'Y** 的顾问备注' })
    await user.clear(note)
    await user.type(note, '客户平移优先')
    await user.tab()
    expect(await card.findByText('备注已保存')).toHaveAttribute('role', 'status')
  })

  it('无目标团队/无候选时给出空态提示', async () => {
    stubFetch(() => ({
      ...taskPayload,
      mapping_task: {
        ...taskPayload.mapping_task,
        target_teams: [],
        candidates: [],
        stats: { ...taskPayload.mapping_task.stats, teams: 0, candidates: 0, confirmed: 0, intaken: 0, clues: 0, failures: [] },
      },
    }))
    const { section } = await renderCard()
    const teams = within(section).getByRole('region', { name: '目标团队' })
    expect(within(teams).getByText('暂无目标团队信息，可返回工作流查看采集说明。')).toBeInTheDocument()
    expect(within(teams).getByText('暂无目标团队')).toBeInTheDocument()
    const candidates = within(section).getByRole('region', { name: '候选目标人' })
    expect(within(candidates).getByText('暂无候选目标人，可返回工作流调整寻访方向。')).toBeInTheDocument()
  })
})

describe('mappingTask 纯展示逻辑', () => {
  it('groupMappingTeams：按公司分组、保序、缺省公司名兜底', () => {
    const groups = groupMappingTeams([
      { company: 'MPS', team: 'PC 方向 TME 团队' },
      { company: '矽力杰', team: '电源 IC 团队' },
      { company: 'MPS', team: 'AE 支持团队' },
      { company: '', team: '无公司团队' },
    ])
    expect(groups.map(group => group.company)).toEqual(['MPS', '矽力杰', '公司待确认'])
    expect(groups[0].teams.map(team => team.team)).toEqual(['PC 方向 TME 团队', 'AE 支持团队'])
    expect(groups[2].teams[0].team).toBe('无公司团队')
    expect(groupMappingTeams([])).toEqual([])
  })

  it('humanizeMappingFailure：机器 reason 说人话，未知透原文附 note', () => {
    expect(humanizeMappingFailure({ source: '官网', reason: 'blocked' })).toBe('官网有反爬保护，没抓到')
    expect(humanizeMappingFailure({ source: '专利', reason: 'timeout' })).toBe('专利访问超时，没抓到')
    expect(humanizeMappingFailure({ source: '猎聘', reason: 'no_site_hint' })).toBe('没有找到猎聘入口，已跳过')
    expect(humanizeMappingFailure({ source: '官网', reason: 'parse_error' })).toBe('官网页面结构变了，没解析出来')
    expect(humanizeMappingFailure({ source: '官网', reason: 'skipped_after_failure', note: '三次失败，跳过' })).toBe('三次失败，跳过')
    expect(humanizeMappingFailure({ source: '官网', reason: 'http_429' })).toBe('官网返回异常（HTTP 429），没抓到')
    expect(humanizeMappingFailure({ source: '官网', reason: 'unknown_x', note: '补充' })).toBe('官网没抓到：unknown_x（补充）')
    expect(humanizeMappingFailure({})).toBe('来源没抓到：原因未知')
  })

  it('mappingClueRate：clues 为 0 不硬编百分比', () => {
    expect(mappingClueRate({ confirmed: 3, clues: 9 })).toEqual({ confirmed: 3, clues: 9, percent: 33 })
    expect(mappingClueRate({ confirmed: 0, clues: 0 })).toEqual({ confirmed: 0, clues: 0, percent: null })
    expect(mappingClueRate({})).toEqual({ confirmed: 0, clues: 0, percent: null })
  })

  it('mappingConfidenceLabel：high/medium/low 中文，未知原文透出', () => {
    expect(mappingConfidenceLabel('high')).toBe('把握大')
    expect(mappingConfidenceLabel('medium')).toBe('把握一般')
    expect(mappingConfidenceLabel('low')).toBe('把握偏小')
    expect(mappingConfidenceLabel('unusual')).toBe('unusual')
    expect(mappingConfidenceLabel(undefined)).toBe('把握待评估')
  })

  it('mappingStatusLabel：七态中文，未知值原文透出', () => {
    expect(mappingStatusLabel('pending')).toBe('待确认')
    expect(mappingStatusLabel('replied')).toBe('已回复')
    expect(mappingStatusLabel('weird')).toBe('weird')
    expect(mappingStatusLabel('')).toBe('待确认')
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
    const onChanged = vi.fn()
    render(<StrategyReviewExpansion workflowId="wf-1" tree={[escalateStep]} jobId={154} onOpenMapping={onOpenMapping} onChanged={onChanged} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '发起 Mapping 直挖' }))
    await waitFor(() => expect(onOpenMapping).toHaveBeenCalledWith('mapping_task_wf-1'))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
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
