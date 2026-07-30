import { describe, it, expect } from 'vitest'
import { buildResumeOverview, parseOverviewBasic, splitIntentKeywords, formatFeedbackScore } from '../panels/overviewFormat'

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

  it('完整简历仅提取头部概览，工作和项目正文不得混入', () => {
    const overview = buildResumeOverview(`牟先生
在职，看看新机会
男37岁苏州-昆山硕士工作12年
运动控制研发工程师江苏烽禾升智能科技有限公司
求职意向
算法工程师
50-60k×12薪
苏州
工作经历
江苏烽禾升智能科技有限公司
职责业绩：负责磁悬浮输送线调度模块算法开发
项目经历
储能客户物流线项目`, {
      name: '牟先生', currentTitle: '运动控制研发工程师', currentCompany: '江苏烽禾升智能科技有限公司', city: '苏州-昆山', education: '硕士', experience: '12年',
    })
    expect(overview.fields).toEqual(expect.arrayContaining([
      { label: '当前职业', value: '运动控制研发工程师 · 江苏烽禾升智能科技有限公司' },
      { label: '工作经验', value: '12年' },
      { label: '学历', value: '硕士' },
    ]))
    expect(overview.intent).toBe('算法工程师')
    expect(overview.tags).toEqual(['50-60k×12薪', '苏州'])
    expect(overview.fallback).toBe('')
    expect(JSON.stringify(overview)).not.toContain('职责业绩')
    expect(JSON.stringify(overview)).not.toContain('储能客户物流线项目')
  })

  it('摘要缺失时只用结构化候选人字段兜底，不回退完整简历', () => {
    const overview = buildResumeOverview('', {
      currentTitle: '算法工程师', currentCompany: '示例科技', city: '上海', education: '本科', experience: '8年',
    })
    expect(overview.fields).toEqual([
      { label: '当前职业', value: '算法工程师 · 示例科技' },
      { label: '工作经验', value: '8年' },
      { label: '学历', value: '本科' },
      { label: '所在地', value: '上海' },
    ])
    expect(overview.fallback).toBe('')
  })
})
