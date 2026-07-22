import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkflowFunnel } from '../workflows/WorkflowFunnel'
import { stepBusinessResult } from '../workflows/utils'
import { zeroAttributionLabel, zeroAttributionLabels } from '../workflow/statusMapping'
import type { Workflow } from '../api'
import { mockResponse } from './helpers'

// R8 渠道漏斗验收：正常渲染 / 六类 0 结果归因中文映射 / 空数据回落 / 数字守恒 / 加载失败降级。

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

  it('正常渲染：每个渠道一行完整漏斗数字与查询明细', async () => {
    stubFunnel({ ok: true, workflow_id: 'wf-1', channels: [liepinChannel], runs: [liepinRun] })
    renderFunnel()
    await screen.findByText('猎聘')
    const line = document.querySelector('.funnel-line')!
    expect(line).toHaveTextContent('查询 2 组')
    expect(line).toHaveTextContent('召回 60')
    expect(line).toHaveTextContent('抽取 24')
    expect(line).toHaveTextContent('排重后 18')
    expect(line).toHaveTextContent('详情 完整 12 / 部分 4 / 失败 2')
    expect(line).toHaveTextContent('入库新增 5（排重命中 13）')
    expect(line).toHaveTextContent('评估 12（高分 3）')
    // 查询明细折叠展开后可见每组 query 与召回数
    await userEvent.click(screen.getByText('查询明细（2 组）'))
    expect(screen.getByText('PC电源 技术市场')).toBeInTheDocument()
    expect(screen.getByText('召回 40 · 抽取 16')).toBeInTheDocument()
  })

  it('数字守恒（fixture 口径）：详情三态合计 ≤ 抽取数；入库新增 ≤ 详情完整数', () => {
    const detailSum = liepinChannel.detail.complete + liepinChannel.detail.partial + liepinChannel.detail.failed
    expect(detailSum).toBeLessThanOrEqual(liepinChannel.extracted_count)
    expect(liepinChannel.intake_new_count).toBeLessThanOrEqual(liepinChannel.detail.complete)
  })

  it('0 结果归因：六类枚举都有中文解释，不渲染英文原形', async () => {
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
