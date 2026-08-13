// 履历"详细工作内容"解析：从 full_text 的工作经历区提取 公司→（期间）→职位→职责业绩 块。
// 实测格式（猎聘完整抓取）：
//   京东方
//   （2021.07 - 至今, 5年）
//   计算机硬件
//   硬件技术经理
//   下属人数：
//   0
//   职责业绩：
//   搭建硬件研发数据库：…
// 解析失败/摘要级履历（如 280 字摘要）→ 返回空数组，调用方回退到标题行展示。

export type WorkDetailBlock = { company: string; period: string; role: string; description: string[] }
export type ProjectDetailBlock = { title: string; period: string; role: string; company: string; description: string[]; duties: string[]; achievements: string[] }
export type EducationDetailBlock = { school: string; major: string; degree: string; period: string; details: string[] }

const PERIOD_RE = /^（[^）]*(\d{4}|至今)[^）]*）$/
// 章节结束：裸标题、标题带（共N段）计数、附件简历/作品集等提示。带正文的“项目经历：xxx”不算，
// 避免把职责正文里的“项目经验：…”误当章节结束（见 parseLiepinWorkBlocks 的 inDuty 规则）。
const SECTION_END_RE = /^(教育经历|项目经历|项目经验|技能(?:特长)?|语言能力|自我评价|附件简历)(?:[：:]?\s*$|（[^）]*）$|｜.*$|\/.*$|与.*$|和.*$)/
const SECTION_END_CONTENT_RE = /^(项目经历|项目经验)[：:]\s*\S/
const SKIP_RE = /^(下属人数|工作地点|汇报对象|所属行业|薪\s*资|薪酬|职位类别|工作时间|合同类型|工作性质|期望薪\s*资|期望工作地点)[:：]?$|^\d+$/
const PROJECT_FIELD_RE = /^(项目职务|所在公司|项目描述|项目职责|项目业绩)[:：]?$/
const XSAAS_PROJECT_FIELD_RE = /^(责任描述|项目简介|内容)[:：]?$/
const BARE_PERIOD_RE = /^\d{4}[./年]\s*\d{1,2}(?:月)?\s*[-至]\s*(?:\d{4}[./年]\s*\d{1,2}(?:月)?|至今)$/
// X-SaaS 坏期间：结束为 0.00/0. 00（“至今”导出残留），或开始为 0.00（离职所需时间残留）。
// 展示层统一按“时间不详”处理，不改动原始数据。
const XSAAS_BAD_PERIOD_RE = /^(?:\d{4}[./年]\s*\d{1,2}(?:月)?\s*[-至]\s*0\.?\s*0+(?:\.?\s*0+)*|0\.?\s*0+\s*[-至]\s*(?:\d{4}[./年]\s*\d{1,2}(?:月)?|至今|0\.?\s*0+(?:\.?\s*0+)*))$/
// X-SaaS 工作经历的字段标题（裸期间行 + 字段标题驱动，区别于猎聘的“公司名 + （期间）”）。
const XSAAS_WORK_FIELD_RE = /^(部门\/职位|汇报对象\/下属|工作内容\/业绩|工作业绩|职位类别|离职所需时间)[：:]?$/
// 路由信号：猎聘履历也会出现“职位类别：”，不能作为 X-SaaS 判型依据，只用强信号字段。
const XSAAS_DETECT_RE = /^(部门\/职位|汇报对象\/下属|工作内容\/业绩|离职所需时间)[：:]?$/
const EDUCATION_DEGREE_RE = /^(博士后|博士|硕士|本科|大专|中专|高中|其它|其他)$/
const EDUCATION_PERIOD_RE = /^(?:\d{4}[./年]\s*\d{1,2}(?:月)?\s*[-至]\s*(?:\d{4}[./年]\s*\d{1,2}(?:月)?|至今)|\d{4}\s*[-至]\s*(?:\d{4}|至今))$/
const SCHOOL_RE = /(?:大学|学院|学校|附中|University|College|School|Institute|Academy|Universit)$/i
const SCHOOL_KEYWORD_RE = /(University|College|School|Institute|Academy|Universit)/i
const NOT_SCHOOL_PREFIX_RE = /^(在校|在读|统招|985|211|双一流|专业|学历|毕业|学位|研究|询问TA|时间|期间|工作|项目)/
const KANGXI_MAP: Record<string, string> = { '\u2F00':'一','\u2F06':'二','\u2F09':'人','\u2F0B':'入','\u2F14':'八','\u2F1A':'十','\u2F1D':'又','\u2F21':'工','\u2F24':'大','\u2F2F':'山','\u2F33':'己','\u2F3E':'女','\u2F3F':'子','\u2F42':'日','\u2F45':'木','\u2F4E':'水','\u2F52':'火','\u2F5F':'田','\u2F66':'白','\u2F6D':'米','\u2F8F':'車','\u2F95':'門','\u2F9A':'馬' }
function normalizeKangxi(token: string): string {
  return [...token].map(ch => KANGXI_MAP[ch] || ch).join('')
}
function isSchoolToken(token: string): boolean {
  const normalized = normalizeKangxi(token)
  const clean = normalized.replace(/[（(][^）)]*[）)]$/g, '').trim()
  if (SCHOOL_RE.test(clean)) return true
  if (SCHOOL_KEYWORD_RE.test(normalized) && !NOT_SCHOOL_PREFIX_RE.test(normalized)) return true
  return false
}

export function parseWorkDetails(fullText: string): WorkDetailBlock[] {
  const text = String(fullText || '')
  let start = text.indexOf('工作经历')
  if (start < 0) start = text.indexOf('工作经验')
  if (start < 0) return []
  const lines = text.slice(start + 4).split('\n').map(l => l.trim()).filter(Boolean)
  const workLines: string[] = []
  for (const line of lines) {
    if (SECTION_END_RE.test(line)) break
    workLines.push(line)
  }
  // X-SaaS 完整履历：裸期间行 + “部门/职位：”等字段标题，没有猎聘的括号期间。
  if (workLines.some(line => XSAAS_DETECT_RE.test(line))) {
    return dedupeWorkBlocks(parseXsaasWorkBlocks(workLines))
  }
  return dedupeWorkBlocks(parseLiepinWorkBlocks(workLines))
}

function parseLiepinWorkBlocks(lines: string[]): WorkDetailBlock[] {
  const blocks: WorkDetailBlock[] = []
  let current: WorkDetailBlock | null = null
  let inDuty = false
  let skipValue = false
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (SECTION_END_RE.test(line) || (!inDuty && SECTION_END_CONTENT_RE.test(line))) break
    const isHeader = i + 1 < lines.length && PERIOD_RE.test(lines[i + 1])
    if (isHeader) {
    if (current) blocks.push(current)
      current = { company: line, period: '', role: '', description: [] }
      inDuty = false
      skipValue = false
      continue
    }
    if (!current) continue
    if (!current.period) {
      if (PERIOD_RE.test(line)) { current.period = line.replace(/[（）]/g, ''); continue }
      blocks.push(current)
      current = null
      continue
    }
    if (line === '职责业绩：' || line === '职责业绩:' || line === '工作业绩：' || line === '工作业绩:') { inDuty = true; continue }
    if (inDuty) { current.description.push(line); continue }
    if (skipValue) { skipValue = false; continue }
    if (SKIP_RE.test(line)) { skipValue = true; continue }
    if (line.includes('：')) { skipValue = true; continue }
    current.role = line  // 行业/职位多行时职位取最后一行
  }
  if (current) blocks.push(current)
  return blocks.filter(b => b.description.length > 0)
}

// X-SaaS 工作经历格式（示例）：
//   2017. 01 - 至今
//   上海微电子装备(集团)股份有限公司
//   部门/职位：
//   专项精密运动部 / 机械设计工程师
//   汇报对象/下属：
//   / 0
//   工作内容/业绩：
//   14薪
//   职位类别：
//   机械结构工程师
//   工作背景：…
function parseXsaasWorkBlocks(lines: string[]): WorkDetailBlock[] {
  const blocks: WorkDetailBlock[] = []
  let current: WorkDetailBlock | null = null
  let mode: '' | 'role' | 'skip' | 'category' | 'duty' = ''
  const push = () => { if (current?.company || current?.period) blocks.push(current) }
  for (const line of lines) {
    if (SECTION_END_RE.test(line) || (mode !== 'duty' && SECTION_END_CONTENT_RE.test(line))) break
    if (BARE_PERIOD_RE.test(line) || XSAAS_BAD_PERIOD_RE.test(line)) {
      push()
      current = { company: '', period: line, role: '', description: [] }
      mode = ''
      continue
    }
    if (!current) continue
    const fieldMatch = line.match(XSAAS_WORK_FIELD_RE)
    if (fieldMatch) {
      mode = fieldMatch[1] === '部门/职位' ? 'role'
        : fieldMatch[1] === '汇报对象/下属' || fieldMatch[1] === '离职所需时间' ? 'skip'
        : fieldMatch[1] === '职位类别' ? 'category'
        : 'duty'
      continue
    }
    if (mode === 'role') { const role = line.replace(/^\/\s*/, '').trim(); current.role = current.role ? `${current.role} ${role}` : role; mode = ''; continue }
    if (mode === 'skip') { mode = ''; continue }
    if (mode === 'category') { mode = 'duty'; continue }
    if (mode === 'duty') {
      if (/^(该段内容已整合附件简历信息|附件简历信息已整合)$/.test(line)) continue
      current.description.push(line)
      continue
    }
    if (!current.company) { current.company = line; continue }
  }
  push()
  return blocks.filter(b => b.company || b.period)
}

// X-SaaS 会把“至今”重复导出成 0.00 结束时间：同一段工作经历出现两份时只保留最好那份
// （优先有效期间、其次有职位）；孤立坏期间按“时间不详”展示，不改动原始数据。
function dedupeWorkBlocks(blocks: WorkDetailBlock[]): WorkDetailBlock[] {
  const grouped = new Map<string, WorkDetailBlock[]>()
  for (const block of blocks) {
    const key = `${block.company}||${block.description.join('|')}`
    const list = grouped.get(key) ?? []
    list.push(block)
    grouped.set(key, list)
  }
  const kept: WorkDetailBlock[] = []
  for (const list of grouped.values()) {
    const best = [...list].sort((a, b) => {
      const aInvalid = XSAAS_BAD_PERIOD_RE.test(a.period) ? 1 : 0
      const bInvalid = XSAAS_BAD_PERIOD_RE.test(b.period) ? 1 : 0
      if (aInvalid !== bInvalid) return aInvalid - bInvalid
      const aRole = String(a.role || '').trim()
      const bRole = String(b.role || '').trim()
      if ((aRole ? 1 : 0) !== (bRole ? 1 : 0)) return (bRole ? 1 : 0) - (aRole ? 1 : 0)
      return 0
    })[0]
    kept.push({ ...best, period: XSAAS_BAD_PERIOD_RE.test(best.period) ? '时间不详' : best.period })
  }
  return kept
}

// 项目经历采用“项目名 + 下一行期间”作为分段锚点；字段标题后的多行文字仍属于同一个项目。
// 不能沿用通用逐行时间轴，否则“项目职责：/内容”会各自变成一个空节点。
export function parseProjectDetails(projectText: string): ProjectDetailBlock[] {
  const lines = String(projectText || '').split('\n').map(line => line.trim()).filter(Boolean)
  const blocks: ProjectDetailBlock[] = []
  let current: ProjectDetailBlock | null = null
  let field = ''
  const push = () => { if (current?.title) blocks.push(current) }
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]
    if (XSAAS_BAD_PERIOD_RE.test(line)) {
      push()
      current = { title: '', period: '时间不详', role: '', company: '', description: [], duties: [], achievements: [] }
      field = ''
      continue
    }
    if (index + 1 < lines.length && PERIOD_RE.test(lines[index + 1])) {
      push()
      current = { title: line, period: '', role: '', company: '', description: [], duties: [], achievements: [] }
      field = ''
      continue
    }
    if (BARE_PERIOD_RE.test(line)) {
      push()
      current = { title: '', period: line, role: '', company: '', description: [], duties: [], achievements: [] }
      field = ''
      continue
    }
    if (!current) continue
    if (!current.period && PERIOD_RE.test(line)) { current.period = line.replace(/[（）]/g, ''); continue }
    if (!current.title && !PROJECT_FIELD_RE.test(line) && !XSAAS_PROJECT_FIELD_RE.test(line)) { current.title = line; continue }
    const fieldMatch = line.match(PROJECT_FIELD_RE)
    if (fieldMatch) { field = fieldMatch[1]; continue }
    const xsaasFieldMatch = line.match(XSAAS_PROJECT_FIELD_RE)
    if (xsaasFieldMatch) { field = xsaasFieldMatch[1]; continue }
    if (field === '项目职务') { current.role = current.role ? `${current.role} ${line}` : line; continue }
    if (field === '所在公司') { current.company = current.company ? `${current.company} ${line}` : line; continue }
    if (field === '项目描述') { current.description.push(line); continue }
    if (field === '项目职责') { current.duties.push(line); continue }
    if (field === '项目业绩') { current.achievements.push(line); continue }
    if (field === '项目简介' || field === '内容') { current.description.push(line); continue }
    if (field === '责任描述') { current.duties.push(line); continue }
  }
  push()
  return blocks
}

// 教育原始文本常以“学校 · 专业 · 学历 · 时间 · 标签”逐行输出。
// 先消掉孤立分隔符，再以学校名切块，避免学历、专业、985/211 被拆成独立时间轴节点。
export function parseEducationDetails(educationText: string): EducationDetailBlock[] {
  const text = String(educationText || '')
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean)
  const hasStructured = lines.some(line => /^专业\s*\/\s*学历[:：]?$/.test(line) || /^专业\s*\/\s*学历[:：]\s*\S/.test(line) || /^(专业|学历|学位)[：:]?$/.test(line))
  if (hasStructured) return parseStructuredEducation(lines)
  return parseTokenEducation(text)
}

// X-SaaS 结构：期间行 → 学校行 → “专业/学历：” → “专业 / 学历”。
// 学校名不做后缀匹配（英文校名常是 University of X），期间行里的 0.00 残留按“时间不详”。
function parseStructuredEducation(lines: string[]): EducationDetailBlock[] {
  const blocks: EducationDetailBlock[] = []
  let current: EducationDetailBlock | null = null
  let field: '' | 'major' | 'degree' = ''
  const push = () => { if (current?.school) blocks.push(current) }
  for (const line of lines) {
    if (EDUCATION_PERIOD_RE.test(line) || XSAAS_BAD_PERIOD_RE.test(line)) {
      push()
      current = { school: '', major: '', degree: '', period: XSAAS_BAD_PERIOD_RE.test(line) ? '时间不详' : line, details: [] }
      field = ''
      continue
    }
    if (!current) continue
    const combined = line.match(/^专业\s*\/\s*学历[:：]\s*(.+)$/)
    if (combined) {
      const match = combined[1].match(/^(.*?)\s*\/\s*(博士后|博士|硕士|本科|大专|中专|高中|其它|其他)$/)
      if (match) { current.major = match[1].trim(); current.degree = match[2] }
      else current.major = combined[1].trim()
      field = ''
      continue
    }
    if (/^专业\s*\/\s*学历[:：]?$/.test(line)) { field = 'major'; continue }
    if (/^(专业|学历|学位)[：:]?$/.test(line)) { field = 'degree'; continue }
    if (field === 'major') {
      const match = line.match(/^(.*?)\s*\/\s*(博士后|博士|硕士|本科|大专|中专|高中|其它|其他)$/)
      if (match) { current.major = match[1].trim(); current.degree = match[2] }
      else current.major = line
      field = ''
      continue
    }
    if (field === 'degree') { current.degree = line; field = ''; continue }
    if (!current.school) { current.school = line; continue }
    current.details.push(line)
  }
  push()
  return blocks
}

// 猎聘点分格式：学校 · 专业 · 学历 · 时间 · 标签；英文校名/带括号注释的校名也能识别。
function parseTokenEducation(text: string): EducationDetailBlock[] {
  const tokens = text
    .split('\n')
    .flatMap(line => line.split('·'))
    .map(token => normalizeKangxi(token.trim()))
    .filter(token => token && token !== '·')
  const blocks: EducationDetailBlock[] = []
  const pending: string[] = []
  let current: EducationDetailBlock | null = null
  const push = () => { if (current?.school) blocks.push(current) }
  const apply = (block: EducationDetailBlock, token: string) => {
    if (/^专业\s*\/\s*学历[:：]?$/.test(token)) return
    if (EDUCATION_PERIOD_RE.test(token)) { block.period = token; return }
    const degreeMatch = token.match(/^(.*)\s*\/\s*(博士后|博士|硕士|本科|大专|中专|高中|其它|其他)$/)
    if (degreeMatch) { block.major = degreeMatch[1].trim(); block.degree = degreeMatch[2]; return }
    if (EDUCATION_DEGREE_RE.test(token)) { block.degree = token; return }
    if (!block.major) { block.major = token; return }
    block.details.push(token)
  }
  for (const token of tokens) {
    if (isSchoolToken(token)) {
      push()
      current = { school: token, major: '', degree: '', period: '', details: [] }
      pending.splice(0).forEach(value => apply(current!, value))
      continue
    }
    if (!current) { pending.push(token); continue }
    if (EDUCATION_PERIOD_RE.test(token) && current.period && (current.major || current.degree)) {
      pending.push(token)
      continue
    }
    apply(current, token)
  }
  push()
  return blocks
}
