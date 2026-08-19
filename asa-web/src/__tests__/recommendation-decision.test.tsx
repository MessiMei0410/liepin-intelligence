import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidatePanel } from '../panels/CandidatePanel'
import type { CandidateDetail } from '../api'
import { candidateDetail, mockResponse } from './helpers'

// 顾问确认推荐（recommendation-decision）：推荐动作保持既有 preflight→commit 语义，
// 追加必填推荐理由 + consultant-recommendations 预检/提交；
// 成功后在详情展示确认状态/理由/确认时间；失败展示可读回执、不关闭对话框、不误报成功。

const preflightUrl = '/api/v1/candidate-actions/preflight'
const commitUrl = '/api/v1/candidate-actions/commit'
const decisionPreflightUrl = '/api/v1/consultant-recommendations/preflight'
const decisionCommitUrl = '/api/v1/consultant-recommendations/commit'
// 写确认链路：candidate-actions commit 前先经 UI 通道激活 token（人确认闸门）。
const activateUrl = '/api/v1/write-confirmations/activate'

const recommendationCandidate = (): CandidateDetail => ({
  ...candidateDetail,
  experience: '8 年',
  education: '本科',
  report_artifacts: [
    { id: 1, artifact_id: 'report-1', artifact_type: 'match_report', title: '匹配报告', validation_status: 'done', version: 1 },
  ],
  sourcing_attributions: [
    {
      id: 1, channel: '猎聘', source_query: '前端工程师', source_round: 'R1', source_purpose: '根据岗位策略生成',
      learning_score: 80, signal_count: 2, review_pass_count: 1, contacted_count: 0, recommended_count: 0,
      stopped_count: 0, client_positive_count: 1, client_rejected_count: 1,
    },
  ],
})

describe('顾问确认推荐（recommendation-decision）', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes(preflightUrl)) return mockResponse({ token: 'tok-1', impact: '将标记该候选人为已推荐' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({ ok: true })
      if (url.includes(decisionPreflightUrl)) return mockResponse({ token: 'consultant-tok-1', impact: '记录顾问确认推荐事实' })
      if (url.includes(decisionCommitUrl)) return mockResponse({ ok: true, confirmed_at: '2026-08-05T14:30:00', reason: '硬性要求匹配，候选人意向已确认' })
      throw new Error(`未预期的请求：${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const openDialog = async (value: CandidateDetail = recommendationCandidate()) => {
    const user = userEvent.setup()
    const changed = vi.fn()
    render(<CandidatePanel value={value} close={() => undefined} changed={changed} />)
    await user.click(screen.getByRole('button', { name: '已推荐' }))
    const dialog = await screen.findByRole('alertdialog')
    return { user, changed, dialog }
  }

  const commitBody = () => {
    const commitCall = fetchMock.mock.calls.find(([input]) => String(input).includes(commitUrl))
    expect(commitCall).toBeDefined()
    return JSON.parse(String((commitCall?.[1] as RequestInit).body)) as Record<string, unknown>
  }

  const decisionCall = () => fetchMock.mock.calls.find(([input]) => String(input).includes(decisionCommitUrl))

  it('候选人详情在长期效果下展示同一查询的多次精确执行', () => {
    const value: CandidateDetail = {
      ...recommendationCandidate(),
      sourcing_recalls: [
        {
          recall_id: 'recall-1',
          run_id: 'run-lineage-7',
          workflow_id: 'wf-lineage-7',
          strategy_hash: 'strategy-hash-7',
          strategy_artifact_id: 'artifact-strategy-7',
          strategy_revision: 7,
          query_plan_hash: 'query-plan-hash-7',
          query_cell_id: 'cell-core-peer-2',
          query_family_ids: ['keyword_group:power'],
          query_provenance: [{ kind: 'keyword_group', tier: 'T1', group: '核心同层', targets: '服务器电源' }],
          channel: 'liepin',
          source_query: '前端工程师',
          page_number: 1,
          position_index: 2,
          created_at: '2026-08-14 03:00:00',
        },
        {
          recall_id: 'recall-2',
          run_id: 'run-lineage-8',
          workflow_id: 'wf-lineage-8',
          strategy_hash: 'strategy-hash-8',
          strategy_artifact_id: 'artifact-strategy-8',
          strategy_revision: 8,
          query_plan_hash: 'query-plan-hash-8',
          query_cell_id: 'cell-core-peer-3',
          query_family_ids: ['company_keyword:Example'],
          query_provenance: [{ kind: 'company_keyword', tier: 'T2', company: '示例科技', path: '相邻行业' }],
          channel: 'liepin',
          source_query: '  前端工程师  ',
          page_number: 2,
          position_index: 1,
          created_at: '2026-08-14 04:00:00',
        },
      ],
    }

    const { container } = render(<CandidatePanel value={value} close={() => undefined} changed={() => undefined} />)

    expect(screen.getByText('寻访来源 · 2 次执行')).toBeInTheDocument()
    expect(container.querySelectorAll('.sourcing-trace-row')).toHaveLength(1)
    expect(container.querySelectorAll('.sourcing-recall')).toHaveLength(2)
    expect(screen.getByText('猎聘 · R1')).toBeInTheDocument()
    expect(screen.getByText('执行 run-lineage-7')).toBeInTheDocument()
    expect(screen.getByText('策略 revision 7 · 单元 cell-core-peer-2')).toBeInTheDocument()
    expect(screen.getByText('关键词组 · T1 · 核心同层 · 服务器电源')).toBeInTheDocument()
    expect(screen.getByText('执行 run-lineage-8')).toBeInTheDocument()
    expect(screen.getByText('策略 revision 8 · 单元 cell-core-peer-3')).toBeInTheDocument()
    expect(screen.getByText('公司定向 · T2 · 示例科技 · 相邻行业')).toBeInTheDocument()
  })

  it('聚合来源缺失时仍显示 recall，旧记录不伪造策略版本', () => {
    const value: CandidateDetail = {
      ...recommendationCandidate(),
      sourcing_attributions: [],
      sourcing_recalls: [{
        recall_id: 'legacy-recall',
        run_id: 'legacy-run',
        query_cell_id: 'legacy-cell',
        channel: 'xsaas',
        source_query: '结构工程师',
        created_at: '2026-08-11 18:36:08',
      }],
    }

    render(<CandidatePanel value={value} close={() => undefined} changed={() => undefined} />)

    expect(screen.getByText('怎么找到他的')).toBeInTheDocument()
    expect(screen.getByText('X-SaaS · 执行记录')).toBeInTheDocument()
    expect(screen.getByText('结构工程师')).toBeInTheDocument()
    expect(screen.getByText('历史记录未保存策略版本 · 单元 legacy-cell')).toBeInTheDocument()
    expect(screen.getByText('待汇总效果')).toBeInTheDocument()
  })

  it('Mapping 公开资料作为独立来源证据展示，不伪造成查询召回', () => {
    const value: CandidateDetail = {
      ...recommendationCandidate(),
      source_links: [{
        source_system: 'mapping',
        source_entity_type: 'external_profile',
        source_entity_id: 'https://example.com/profile/9',
        source_url: 'https://example.com/profile/9',
      }],
      sourcing_attributions: [],
      sourcing_recalls: [],
    }

    render(<CandidatePanel value={value} close={() => undefined} changed={() => undefined} />)

    expect(screen.getByText('来源证据 · 0 次寻访执行 · Mapping 直挖')).toBeInTheDocument()
    expect(screen.getByText('从 Mapping 任务卡确认后入库')).toBeInTheDocument()
    expect(screen.getByText(/不作为猎聘\/X-SaaS 查询召回；任务卡与候选索引已保留/)).toBeInTheDocument()
    expect(screen.getByText('待完整简历复核')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /核对公开资料/ })).toHaveAttribute('href', 'https://example.com/profile/9')
    expect(screen.getByRole('link', { name: /Mapping 公开资料/ })).toHaveAttribute('href', 'https://example.com/profile/9')
    expect(screen.queryByText('猎聘 · 执行记录')).not.toBeInTheDocument()
  })

  it('provenance 缺失时回显查询族，有哈希无 revision 时仅声明已批准', () => {
    const value: CandidateDetail = {
      ...recommendationCandidate(),
      sourcing_attributions: [],
      sourcing_recalls: [{
        recall_id: 'family-recall',
        run_id: 'family-run',
        strategy_hash: 'approved-hash',
        query_cell_id: 'family-cell',
        query_family_ids: ['keyword_group:power', 'company_keyword:Example'],
        channel: 'liepin',
        source_query: '服务器电源',
      }],
    }

    render(<CandidatePanel value={value} close={() => undefined} changed={() => undefined} />)

    expect(screen.getByText('查询族 · keyword_group:power · company_keyword:Example')).toBeInTheDocument()
    expect(screen.getByText('已批准策略 · 单元 family-cell')).toBeInTheDocument()
    expect(screen.queryByText(/^\u7b56略 ·/)).not.toBeInTheDocument()
  })

  it('推荐对话框展示评估依据与风险提示（来自既有候选人字段）', async () => {
    const { dialog } = await openDialog()
    expect(within(dialog).getByText('8 年 · 本科')).toBeInTheDocument()
    expect(within(dialog).getByText('前端工程师 @ 示例科技')).toBeInTheDocument()
    expect(within(dialog).getByText('1 项匹配/推荐报告')).toBeInTheDocument()
    expect(within(dialog).getByText('客户正向 1')).toBeInTheDocument()
    expect(within(dialog).getByText(/曾有 1 次客户否决记录/)).toBeInTheDocument()
    expect(within(dialog).getByText(/暂无已联系记录/)).toBeInTheDocument()
  })

  it('推荐理由必填：为空时确认禁用，填写后可用', async () => {
    const { user, dialog } = await openDialog()
    const confirm = within(dialog).getByRole('button', { name: '确认标记已推荐' })
    expect(confirm).toBeDisabled()
    await user.type(within(dialog).getByRole('textbox', { name: /推荐理由/ }), '硬性要求匹配，候选人意向已确认')
    expect(confirm).toBeEnabled()
  })

  it('常用理由快捷按钮填入推荐理由', async () => {
    const { user, dialog } = await openDialog()
    await user.click(within(dialog).getByRole('button', { name: '硬性匹配' }))
    expect(within(dialog).getByRole('textbox', { name: /推荐理由/ })).toHaveValue('硬性要求匹配，候选人意向已确认，建议推进推荐')
  })

  it('确认提交：commit 后幂等写决定记录，成功后详情展示确认状态/理由/时间', async () => {
    const { user, changed, dialog } = await openDialog()
    await user.type(within(dialog).getByRole('textbox', { name: /推荐理由/ }), '硬性要求匹配，候选人意向已确认')
    await user.click(within(dialog).getByRole('button', { name: '确认标记已推荐' }))

    expect(await screen.findByText(/推荐理由与确认时间已记录/)).toBeInTheDocument()
    expect(commitBody()).toMatchObject({
      candidate_id: 1,
      action: 'recommend',
      preflight_token: 'tok-1',
      note: '硬性要求匹配，候选人意向已确认',
    })

    const decision = decisionCall()
    expect(decision).toBeDefined()
    const init = decision?.[1] as RequestInit
    expect(init.method).toBe('POST')
    const headers = init.headers as Record<string, string>
    expect(headers['Idempotency-Key']).toContain(decisionCommitUrl)
    expect(JSON.parse(String(init.body))).toMatchObject({
      candidate_id: 1,
      reason: '硬性要求匹配，候选人意向已确认',
      preflight_token: 'consultant-tok-1',
    })
    expect(JSON.parse(String(init.body))).toHaveProperty('request_id')

    expect(changed).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.getByText('已确认推荐')).toBeInTheDocument()
    expect(screen.getByText(/硬性要求匹配，候选人意向已确认（确认时间 2026-08-05 14:30）/)).toBeInTheDocument()
    expect(screen.getByText(/2026-08-05 14:30/)).toBeInTheDocument()
  })

  it('决定记录失败：显示可读回执、对话框保持打开、不显示确认成功；重试后成功', async () => {
    let decisionFailed = true
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(preflightUrl)) return mockResponse({ token: 'tok-1', impact: '将标记该候选人为已推荐' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({ ok: true })
      if (url.includes(decisionPreflightUrl)) return mockResponse({ token: 'consultant-tok-1', impact: '记录顾问确认推荐事实' })
      if (url.includes(decisionCommitUrl)) {
        if (decisionFailed) return mockResponse({ detail: '推荐决定写入失败：数据库不可用' }, false, 500)
        return mockResponse({ ok: true, confirmed_at: '2026-08-05T15:00:00', reason: '匹配度高' })
      }
      throw new Error(`未预期的请求：${url}`)
    })
    const { user, dialog } = await openDialog()
    await user.type(within(dialog).getByRole('textbox', { name: /推荐理由/ }), '匹配度高')
    await user.click(within(dialog).getByRole('button', { name: '确认标记已推荐' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/推荐理由确认记录失败：推荐决定写入失败：数据库不可用/)).toBeInTheDocument()
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    expect(screen.queryByText(/已确认推荐/)).not.toBeInTheDocument()

    decisionFailed = false
    await user.click(within(dialog).getByRole('button', { name: '确认标记已推荐' }))
    expect(await screen.findByText(/推荐理由与确认时间已记录/)).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.getByText(/2026-08-05 15:00/)).toBeInTheDocument()
  })

  it('后端判定此前已完成时显示同步回执且仍记录理由', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes(preflightUrl)) return mockResponse({ token: 'tok-1', impact: '将标记该候选人为已推荐' })
      if (url.includes(activateUrl)) return mockResponse({ ok: true, activated: true })
      if (url.includes(commitUrl)) return mockResponse({ ok: true, already_applied: true, stage: 'S7 已推荐客户/待反馈' })
      if (url.includes(decisionPreflightUrl)) return mockResponse({ token: 'consultant-tok-1', impact: '记录顾问确认推荐事实' })
      if (url.includes(decisionCommitUrl)) return mockResponse({ ok: true, already_confirmed: true, confirmed_at: '2026-08-05T10:00:00', reason: '历史确认理由' })
      throw new Error(`未预期的请求：${url}`)
    })
    const { user, dialog } = await openDialog()
    await user.type(within(dialog).getByRole('textbox', { name: /推荐理由/ }), '匹配度高')
    await user.click(within(dialog).getByRole('button', { name: '确认标记已推荐' }))
    expect(await screen.findByText(/此前已完成，已同步当前候选人状态（S7 已推荐客户\/待反馈），推荐理由已确认记录/)).toBeInTheDocument()
  })

  it('候选人字段缺省时评估依据如实呈现、不崩溃', async () => {
    const value = {
      ...candidateDetail,
      experience: undefined,
      education: undefined,
      current_title: undefined,
      current_company: undefined,
      sourcing_attributions: undefined,
      report_artifacts: undefined,
    } as unknown as CandidateDetail
    const { dialog } = await openDialog(value)
    expect(within(dialog).getByText('- · -')).toBeInTheDocument()
    expect(within(dialog).getByText(/经验\/学历字段缺失/)).toBeInTheDocument()
    expect(within(dialog).getByText('暂无匹配/推荐报告')).toBeInTheDocument()
  })

  it('已停止候选人不出现推荐入口（不 bypass 停止保护）', async () => {
    render(<CandidatePanel value={{ ...candidateDetail, is_stopped: true, stop_reason_label: '意向不足' }} close={() => undefined} changed={() => undefined} />)
    expect(screen.queryByRole('button', { name: '已推荐' })).not.toBeInTheDocument()
    expect(screen.getByText(/已停止推进 · 意向不足/)).toBeInTheDocument()
  })
})
