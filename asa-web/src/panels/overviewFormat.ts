// 概览 tab 展示格式化（UX-1：业务语言、结构清晰）。纯函数，供 CandidatePanel 与测试复用。

export type OverviewBasic = { age?: string; experience?: string; education?: string; city?: string; status?: string; fallback: string }

// 基本信息行原始形态（实测）："在线 邓先生 41岁 工作15年 硕士 杭州" / "隐藏 活跃状态 唐** 33岁 工作9年 硕士 苏州-吴中区"
// 解析失败时 fallback 为原文（ joined ），不丢信息。
export function parseOverviewBasic(basic: string): OverviewBasic {
  const text = basic.trim()
  const match = text.match(/(\d+岁)\s+工作(\d+年)\s+(\S+)\s+(\S+)/)
  if (!match) return { fallback: text }
  const status = /^(在线|离线)/.test(text) ? text.match(/^(在线|离线)/)?.[1] : undefined
  return { age: match[1], experience: `${match[2]}经验`, education: match[3], city: match[4], status, fallback: text }
}

// "求职期望："后文本（实测）："杭州市场总监 硬件开发 FPGA LTSpice …" / "苏州区域销售经理/主管"
// 首词为意向（城市+职位连写），其余为技能关键词；只有一词时整体作意向。
export function splitIntentKeywords(raw: string): { intent: string; keywords: string[] } {
  const tokens = raw.split(/\s+/).filter(Boolean)
  if (tokens.length <= 1) return { intent: tokens[0] || '', keywords: [] }
  return { intent: tokens[0], keywords: tokens.slice(1) }
}

// 历史反馈得分：正绿负红，0 不显示（经验分 → 反馈得分，带符号）
export function formatFeedbackScore(raw: unknown): { text: string; tone: 'positive' | 'negative' | 'muted' } {
  const score = Number(raw || 0)
  if (!score) return { text: '', tone: 'muted' }
  return { text: `反馈得分 ${score > 0 ? '+' : ''}${score.toFixed(1)}`, tone: score > 0 ? 'positive' : 'negative' }
}
