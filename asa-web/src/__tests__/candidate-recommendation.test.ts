import { describe, expect, it } from 'vitest'
import { candidateRecommendationLabel, candidateRecommendationTone } from '../shared/candidateRecommendation'

describe('候选人推进建议展示', () => {
  it.each([
    ['recommended', '推荐', 'good'],
    ['priority_review', '优先复核', 'warn'],
    ['verify_first', '待补证据', 'warn'],
    ['not_recommended', '不推荐', 'bad'],
    ['hold', '暂缓', 'warn'],
    ['pending_review', '待复核', 'neutral'],
  ] as const)('%s 映射为 %s', (value, label, tone) => {
    expect(candidateRecommendationLabel(value)).toBe(label)
    expect(candidateRecommendationTone(value)).toBe(tone)
  })

  it('高分不参与结论推断，缺失与未知英文值都保持待确认语义', () => {
    expect(candidateRecommendationLabel()).toBe('待复核')
    expect(candidateRecommendationLabel('future_internal_state')).toBe('待确认')
    expect(candidateRecommendationTone('future_internal_state')).toBe('neutral')
  })

  it('后端已提供中文业务结论时原样保留', () => {
    expect(candidateRecommendationLabel('建议优先复核')).toBe('建议优先复核')
  })
})
