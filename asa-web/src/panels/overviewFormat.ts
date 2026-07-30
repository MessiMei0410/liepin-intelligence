// 概览 tab 展示格式化（UX-1：业务语言、结构清晰）。纯函数，供 CandidatePanel 与测试复用。

export type OverviewBasic = { age?: string; experience?: string; education?: string; city?: string; status?: string; fallback: string }
export type ResumeOverviewField = { label: string; value: string }
export type ResumeOverview = { fields: ResumeOverviewField[]; intent: string; tags: string[]; fallback: string }

type CandidateOverviewSource = {
  name?: string
  currentCompany?: string
  currentTitle?: string
  city?: string
  education?: string
  experience?: string
}

const RESUME_SECTION_RE = /^(工作经历|工作经验|项目经历|项目经验|教育经历|教育背景|技能(?:特长)?|语言能力|自我评价|附件简历)/
const INTENT_HEADING_RE = /^求职(?:意向|期望)[:：]?$/
const VIEW_ALL_RE = /^查看全部(?:\s*\(\d+\))?$/

function cleanLines(text: string): string[] {
  const lines = String(text || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  const sectionIndex = lines.findIndex(line => RESUME_SECTION_RE.test(line))
  return (sectionIndex >= 0 ? lines.slice(0, sectionIndex) : lines).filter(line => !VIEW_ALL_RE.test(line))
}

function unique(values: string[]): string[] {
  return [...new Set(values.map(value => value.trim()).filter(Boolean))]
}

// 职业概览只消费履历头部的身份、状态和意向；工作/项目/教育正文必须留在“履历”页。
// 这层也兜住旧数据中 profile_text 与 full_text 相同的情况，避免任何候选人回退成简历长文。
export function buildResumeOverview(text: string, source: CandidateOverviewSource): ResumeOverview {
  const lines = cleanLines(text)
  const inlineIntent = lines.find(line => /求职(?:意向|期望)[:：]/.test(line)) || ''
  const intentIndex = lines.findIndex(line => INTENT_HEADING_RE.test(line))
  const intentLines = intentIndex >= 0
    ? lines.slice(intentIndex + 1)
    : inlineIntent
      ? inlineIntent.split(/求职(?:意向|期望)[:：]/)[1]?.trim().split(/\s+/).filter(Boolean) || []
      : []
  const basicLines = lines.filter((line, index) => index !== intentIndex && line !== inlineIntent)
  const basicText = basicLines.join(' ')
  const parsed = parseOverviewBasic(basicText)
  const rawAge = basicText.match(/(?:男|女)?\s*(\d{1,2})岁/)?.[1]
  const rawExperience = basicText.match(/(?:工作|从业|经验)\s*(\d{1,2})年/)?.[1]
  const rawEducation = basicText.match(/(博士后|博士|硕士|本科|大专|中专|高中)/)?.[1]
  const status = basicLines.find(line => /在职|离职|到岗|机会|求职|看新机会|暂不考虑/.test(line) && line.length <= 40 && !/求职(?:意向|期望)/.test(line)) || parsed.status || ''
  const fields: ResumeOverviewField[] = []
  const currentRole = [source.currentTitle, source.currentCompany].filter(Boolean).join(' · ')
  if (currentRole) fields.push({ label: '当前职业', value: currentRole })
  const experience = source.experience || (rawExperience ? `${rawExperience}年经验` : parsed.experience)
  if (experience) fields.push({ label: '工作经验', value: experience })
  const education = source.education || rawEducation || parsed.education
  if (education) fields.push({ label: '学历', value: education })
  const city = source.city || parsed.city
  if (city) fields.push({ label: '所在地', value: city })
  if (status) fields.push({ label: '求职状态', value: status })
  if (rawAge || parsed.age) fields.push({ label: '年龄', value: rawAge ? `${rawAge}岁` : parsed.age || '' })

  const intent = intentLines[0] || ''
  const tags = unique(intentLines.slice(1)).slice(0, 6)
  const fallback = fields.length
    ? ''
    : basicLines
      .filter(line => line !== source.name && line !== status && !/\d{1,2}岁|(?:工作|从业|经验)\s*\d{1,2}年|^(?:男|女)/.test(line))
      .find(line => line.length <= 160 && !line.includes(source.currentCompany || '__never__')) || ''
  return { fields, intent, tags, fallback }
}

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
