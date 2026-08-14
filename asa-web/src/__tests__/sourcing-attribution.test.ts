import { describe, expect, it } from 'vitest'
import { sourcingAttributionChannel, sourcingAttributionQuery, sourcingAttributionRound, sourcingAttributionStatus } from '../workflow/sourcingAttribution'

describe('寻访归因展示', () => {
  it('保留本轮/历史状态、渠道、关键词和轮次', () => {
    const attribution = { channel: 'liepin', source_query: '服务器电源 技术市场', source_round: 'R3', from_workflow: true }
    expect(sourcingAttributionStatus(attribution)).toBe('本轮新增')
    expect(sourcingAttributionChannel(attribution)).toBe('猎聘')
    expect(sourcingAttributionQuery(attribution)).toBe('关键词：服务器电源 技术市场')
    expect(sourcingAttributionRound(attribution)).toBe(' · R3')
  })

  it('缺失查询词时明确标为未记录，不猜测策略', () => {
    const attribution = { channel: 'xsaas', source_round: '', from_workflow: false }
    expect(sourcingAttributionStatus(attribution)).toBe('历史入库')
    expect(sourcingAttributionChannel(attribution)).toBe('X-SaaS')
    expect(sourcingAttributionQuery(attribution)).toBe('关键词未记录')
    expect(sourcingAttributionRound(attribution)).toBe('')
  })
})
