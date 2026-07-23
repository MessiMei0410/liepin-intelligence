import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkflowFunnel } from '../workflows/WorkflowFunnel'
import { stepBusinessResult } from '../workflows/utils'
import { zeroAttributionLabel, zeroAttributionLabels } from '../workflow/statusMapping'
import type { Workflow } from '../api'
import { mockResponse } from './helpers'

// R8 渠道漏斗验收：正常渲染 / 0 结果归因中文映射（含 S4-3c-1 新增两类）/ 空数据回落 / 数字守恒 / 加载失败降级。

const liepinRun = {
  run_id: 'r1', channel: 'liepin', status: 'completed', query_count: 2,
  queries: [
    { query: 'PC电源 技术市场', result_count: 40, extracted_count: 16 },
    { query: '电源 市场经理', result_count: 20, extracted_count: 8 },
  ],
  recall_count: 60, extracted_count: 24, dedupe_count: 6, unique_count: 18,
  intake_duplicate_count: 13, intake_new_count: 5, assessed_count: 12, high_score_count: 3,
  detail: { complete: 12, partial: 4, failed: 2, complete_rate: 0.6667 },
  zero_attribution: null, error: null, created_at: '2026-07-22 10:00:00',
}
const liepinChannel = {
  channel: 'liepin', runs: 1, status: 'completed',
  recall_count: 60, extracted_count: 24, dedupe_count: 6, unique_count: 18,
  intake_duplicate_count: 13, intake_new_count: 5, assessed_count: 12, high_score_count: 3,
  detail: { complete: 12, partial: 4, failed: 2, complete_rate: 0.6667 },
  zero_attribution: null,
}
// 第二条渠道 fixture（非零结果），用于“两行渠道行”渲染与逐行数字断言。
const xsaasChannel = {
  channel: 'xsaas', runs: 1, status: 'completed',
  recall_count: 30, extracted_count: 20, dedupe_count: 4, unique_count: 16,
  intake_duplicate_count: 4, intake_new_count: 8, assessed_count: 9, high_score_count: 4,
  detail: { complete: 10, partial: 2, failed: 1, complete_rate: 0.7692 },
  zero_attribution: null,
}
const xsaasRun = {
  run_id: 'r2', channel: 'xsaas', status: 'completed', query_count: 1,
  queries: [{ query: 'PC电源 市场', result_count: 30, extracted_count: 20 }],
  recall_count: 30, extracted_count: 20, dedupe_count: 4, unique_count: 16,
  intake_duplicate_count: 4, intake_new_count: 8, assessed_count: 9, high_score_count: 4,
  detail: { complete: 10, partial: 2, failed: 1, complete_rate: 0.7692 },
  zero_attribution: null, error: null, created_at: '2026-07-22 10:05:00',
}
const zeroChannel = (code: string) => ({
  channel: 'xsaas', runs: 1, status: 'completed',
  recall_count: 0, extracted_count: 0, dedupe_count: 0, unique_count: 0,
  intake_duplicate_count: 0, intake_new_count: 0, assessed_count: 0, high_score_count: 0,
  detail: { complete: 0, partial: 0, failed: 0, complete_rate: null },
  zero_attribution: code,
})
const zeroRun = (code: string, error: string | null = null) => ({
  run_id: 'r1', channel: 'xsaas', status: 'completed', query_count: 1,
  queries: [{ query: '电源', result_count: 0, extracted_count: 0 }],
  recall_count: 0, extracted_count: 0, dedupe_count: 0, unique_count: 0,
  intake_duplicate_count: 0, intake_new_count: 0, assessed_count: 0, high_score_count: 0,
  detail: { complete: 0, partial: 0, failed: 0, complete_rate: null },
  zero_attribution: code, error, created_at: '2026-07-22 10:00:00',
})

const stubFunnel = (payload: unknown) => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/sourcing-funnel')) return Promise.resolve(mockResponse(payload))
    return Promise.resolve(mockResponse({ ok: true }))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const renderFunnel = () => render(<WorkflowFunnel workflowId="wf-1" updatedAt="2026-07-22 10:00:00" />)

describe('R8 渠道寻访漏斗', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('正常渲染：两行渠道行，每行完整漏斗数字逐项出现', async () => {
    const fetchMock = stubFunnel({ ok: true, workflow_id: 'wf-1', channels: [liepinChannel, xsaasChannel], runs: [liepinRun, xsaasRun] })
    const { container } = renderFunnel()
    await screen.findByText('猎聘')
    // api 层路由锚定：打的是 /sourcing-funnel 按需路由
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/workflows/wf-1/sourcing-funnel')
    const rows = container.querySelectorAll('.funnel-channel')
    expect(rows).toHaveLength(2)
    const liepinLine = rows[0].querySelector('.funnel-line')!
    expect(rows[0]).toHaveTextContent('猎聘')
    expect(liepinLine).toHaveTextContent('查询 2 组')
    expect(liepinLine).toHaveTextContent('召回 60')
    expect(liepinLine).toHaveTextContent('抽取 24')
    expect(liepinLine).toHaveTextContent('排重后 18')
    expect(liepinLine).toHaveTextContent('详情（完整 12 / 部分 4 / 失败 2）')
    expect(liepinLine).toHaveTextContent('入库新增 5（排重命中 13）')
    expect(liepinLine).toHaveTextContent('评估 12（高分 3）')
    const xsaasLine = rows[1].querySelector('.funnel-line')!
    expect(rows[1]).toHaveTextContent('X-SaaS')
    expect(xsaasLine).toHaveTextContent('查询 1 组')
    expect(xsaasLine).toHaveTextContent('召回 30')
    expect(xsaasLine).toHaveTextContent('详情（完整 10 / 部分 2 / 失败 1）')
    expect(xsaasLine).toHaveTextContent('入库新增 8（排重命中 4）')
    expect(xsaasLine).toHaveTextContent('评估 9（高分 4）')
    // 查询明细折叠展开后可见每组 query 与召回数
    await userEvent.click(screen.getByText('查询明细（2 组）'))
    expect(screen.getByText('PC电源 技术市场')).toBeInTheDocument()
    expect(screen.getByText('召回 40 · 抽取 16')).toBeInTheDocument()
  })

  // 渲染行数字链：查询 N 组 → 召回 X → 抽取 Y → 排重后 Z → 详情（完整 a / 部分 b / 失败 c）→ 入库新增 e（排重命中 d）→ 评估 f（高分 g）
  const FUNNEL_LINE_PATTERN = /查询 (\d+) 组\s*→\s*召回 (\d+)\s*→\s*抽取 (\d+)\s*→\s*排重后 (\d+)\s*→\s*详情（完整 (\d+) \/ 部分 (\d+) \/ 失败 (\d+)）\s*→\s*入库新增 (\d+)（排重命中 (\d+)）\s*→\s*评估 (\d+)（高分 (\d+)）/

  it('数字守恒（渲染侧）：详情三态合计 ≤ 抽取数；入库新增 ≤ 详情完整数', async () => {
    stubFunnel({ ok: true, workflow_id: 'wf-1', channels: [liepinChannel, xsaasChannel], runs: [liepinRun, xsaasRun] })
    const { container } = renderFunnel()
    await screen.findByText('猎聘')
    const lines = container.querySelectorAll('.funnel-line')
    expect(lines).toHaveLength(2)
    for (const line of lines) {
      const match = line.textContent.match(FUNNEL_LINE_PATTERN)
      expect(match, `漏斗行格式不符：${line.textContent}`).not.toBeNull()
      const [, , , extracted, , complete, partial, failed, intakeNew] = match!.map(Number)
      expect(complete + partial + failed).toBeLessThanOrEqual(extracted)
      expect(intakeNew).toBeLessThanOrEqual(complete)
    }
    // fixture 口径自证：合法 fixture 自身必须满足守恒，否则上面的渲染断言失去意义
    const detailSum = liepinChannel.detail.complete + liepinChannel.detail.partial + liepinChannel.detail.failed
    expect(detailSum).toBeLessThanOrEqual(liepinChannel.extracted_count)
    expect(liepinChannel.intake_new_count).toBeLessThanOrEqual(liepinChannel.detail.complete)
  })

  it('非法 fixture：违反守恒的数字原样透出（组件不做数值截断），不白屏不抛错', async () => {
    // 架构约定（sourcingFunnel.ts）：边界 parse 只归一化结构，渲染层信任口径、不做语义截断——
    // 语义违规（详情三态合计 > 抽取数、入库新增 > 完整数、负计数）原样渲染，让数据 bug 在面板上可见，
    // 而不是被前端静默掩盖；守恒保障由上一用例的渲染侧断言 + 后端写入口径承担。
    const broken = {
      ...liepinChannel,
      recall_count: -5,
      extracted_count: 10,
      intake_new_count: 15,
      detail: { complete: 9, partial: 9, failed: 9, complete_rate: 0.3333 },
    }
    stubFunnel({ ok: true, channels: [broken], runs: [liepinRun] })
    const { container } = renderFunnel()
    await screen.findByText('猎聘')
    expect(container.querySelector('.funnel-line')).toHaveTextContent('详情（完整 9 / 部分 9 / 失败 9）')
    expect(container.querySelector('.funnel-line')).toHaveTextContent('入库新增 15')
  })

  it('边界防御：detail 缺失归一化为 0，channels 非数组归一化为空明细', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    stubFunnel({ ok: true, channels: [{ channel: 'liepin', runs: 1, recall_count: 3, extracted_count: 2, unique_count: 2, intake_duplicate_count: 0, intake_new_count: 1, assessed_count: 1, high_score_count: 0 }], runs: [] })
    const { container, unmount } = renderFunnel()
    await screen.findByText('猎聘')
    expect(container.querySelector('.funnel-line')).toHaveTextContent('详情（完整 0 / 部分 0 / 失败 0）')
    expect(warn).toHaveBeenCalled()
    unmount()
    stubFunnel({ ok: true, channels: null, runs: 'oops' })
    renderFunnel()
    expect(await screen.findAllByText(/该轮未记录渠道明细/)).not.toHaveLength(0)
  })

  it('0 结果归因映射与 ROUND2 T2 映射表逐字一致（六枚举冻结 + S4-3c-1 新增两类）', () => {
    expect(zeroAttributionLabels).toEqual({
      session_expired: '登录态失效，需重新登录该渠道',
      loading_incomplete: '页面加载未完成或查询未生效',
      page_structure_changed: '页面结构变化，解析器需要适配',
      parse_failure: '平台有结果但解析抓取失败',
      no_results: '该渠道真实无匹配结果',
      query_build_error: '查询构造异常',
      pool_saturated: '本地人才库基本找遍了（重复率太高）',
      unknown: '原因待排查',
    })
  })

  it('0 结果归因：每类枚举都有中文解释，不渲染英文原形', async () => {
    for (const code of Object.keys(zeroAttributionLabels)) {
      stubFunnel({ ok: true, channels: [zeroChannel(code)], runs: [zeroRun(code)] })
      const { container, unmount } = renderFunnel()
      await screen.findByText('X-SaaS')
      expect(screen.getByText(zeroAttributionLabels[code])).toBeInTheDocument()
      expect(container).not.toHaveTextContent(` ${code} `)
      unmount()
    }
  })

  it('no_results 为信息态（muted），其余归因为告警态（warn）', async () => {
    stubFunnel({ ok: true, channels: [zeroChannel('no_results')], runs: [zeroRun('no_results')] })
    const { container, unmount } = renderFunnel()
    await screen.findByText(zeroAttributionLabels.no_results)
    expect(container.querySelector('.funnel-channel')!.className).toContain('muted')
    unmount()
    stubFunnel({ ok: true, channels: [zeroChannel('parse_failure')], runs: [zeroRun('parse_failure')] })
    const { container: second } = renderFunnel()
    await screen.findByText(zeroAttributionLabels.parse_failure)
    expect(second.querySelector('.funnel-channel')!.className).toContain('warn')
  })

  it('unknown 归因附最近错误摘要', async () => {
    stubFunnel({ ok: true, channels: [zeroChannel('unknown')], runs: [zeroRun('unknown', 'boom: unexpected runner state')] })
    renderFunnel()
    await screen.findByText(zeroAttributionLabels.unknown)
    expect(screen.getByText(/最近错误：boom/)).toBeInTheDocument()
  })

  it('空数据回落：历史轮次显示"该轮未记录渠道明细"，不报错不空白', async () => {
    stubFunnel({ ok: true, workflow_id: 'wf-1', channels: [], runs: [] })
    renderFunnel()
    expect(await screen.findAllByText(/该轮未记录渠道明细/)).not.toHaveLength(0)
  })

  it('接口失败降级为提示，不白屏不抛错', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(mockResponse({ error: 'boom' }, false, 500)))
    vi.stubGlobal('fetch', fetchMock)
    renderFunnel()
    await waitFor(() => expect(screen.getByText(/明细加载失败/)).toBeInTheDocument())
  })
})

describe('R8 步骤结果不再把 0 结果显示为 completed 成功', () => {
  const sourcingStep = (runs: Array<Record<string, unknown>>): Workflow['steps'][number] => ({
    id: 2, sequence: 2, business_label: '执行多渠道寻访', risk_level: '中', status: 'completed',
    output: { external_result: { channel_runs: runs } },
  })

  it('0 候选且有质量标记/归因 → 显示"0 条候选 · 归因"，不显示 completed', () => {
    const result = stepBusinessResult(sourcingStep([
      { channel: 'liepin', status: 'completed', result: { ok: true, candidates: 5 } },
      { channel: 'xsaas', status: 'completed', result: { ok: true, candidates: 0 }, quality: 'zero_attributed', zero_attribution: 'session_expired' },
    ]))
    const line = result.facts.find(fact => fact.startsWith('渠道结果')) || ''
    expect(line).toContain('猎聘 5 条候选')
    expect(line).toContain('X-SaaS 0 条候选 · 登录态失效')
    expect(line).not.toContain('xsaas completed')
    expect(line).not.toContain('X-SaaS completed')
  })

  it('0 候选但无质量标记（旧数据）→ 回落状态文案，不编造归因', () => {
    const result = stepBusinessResult(sourcingStep([
      { channel: 'xsaas', status: 'blocked', result: { ok: false, candidates: 0 } },
    ]))
    const line = result.facts.find(fact => fact.startsWith('渠道结果')) || ''
    expect(line).toContain('X-SaaS blocked')
  })

  it('zeroAttributionLabel 未知枚举回落"待排查"并保留原值', () => {
    expect(zeroAttributionLabel('brand_new_code')).toBe('待排查（brand_new_code）')
    expect(zeroAttributionLabel('')).toBe('')
    expect(zeroAttributionLabel(null)).toBe('')
  })
})
