import { describe, it, expect } from 'vitest'
import { parseOverviewBasic, splitIntentKeywords, formatFeedbackScore } from '../panels/overviewFormat'

describe('概览排版格式化', () => {
  it('基本信息：标准形态解析（在线 邓先生 41岁 工作15年 硕士 杭州）', () => {
    expect(parseOverviewBasic('在线 邓先生 41岁 工作15年 硕士 杭州')).toEqual({
      age: '41岁', experience: '15年经验', education: '硕士', city: '杭州', status: '在线',
      fallback: '在线 邓先生 41岁 工作15年 硕士 杭州',
    })
  })

  it('基本信息：连字符城市与无状态（隐藏 活跃状态 唐** 33岁 工作9年 硕士 苏州-吴中区）', () => {
    const f = parseOverviewBasic('隐藏 活跃状态 唐** 33岁 工作9年 硕士 苏州-吴中区')
    expect(f.age).toBe('33岁')
    expect(f.experience).toBe('9年经验')
    expect(f.education).toBe('硕士')
    expect(f.city).toBe('苏州-吴中区')
    expect(f.status).toBeUndefined()
  })

  it('基本信息：异常输入回退原文不丢信息', () => {
    expect(parseOverviewBasic('').fallback).toBe('')
    expect(parseOverviewBasic('一些无法解析的文本').fallback).toBe('一些无法解析的文本')
  })

  it('意向/关键词拆分：首词意向，其余关键词', () => {
    expect(splitIntentKeywords('杭州市场总监 硬件开发 FPGA LTSpice')).toEqual({
      intent: '杭州市场总监',
      keywords: ['硬件开发', 'FPGA', 'LTSpice'],
    })
    expect(splitIntentKeywords('苏州区域销售经理/主管')).toEqual({ intent: '苏州区域销售经理/主管', keywords: [] })
    expect(splitIntentKeywords('')).toEqual({ intent: '', keywords: [] })
  })

  it('反馈得分：正/负/零三态', () => {
    expect(formatFeedbackScore(2)).toEqual({ text: '反馈得分 +2.0', tone: 'positive' })
    expect(formatFeedbackScore(-1.5)).toEqual({ text: '反馈得分 -1.5', tone: 'negative' })
    expect(formatFeedbackScore(0)).toEqual({ text: '', tone: 'muted' })
  })
})
