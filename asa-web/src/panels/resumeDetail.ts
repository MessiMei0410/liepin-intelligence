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
const SECTION_END_RE = /^(教育经历|项目经历|项目经验|技能|语言能力|自我评价|附件简历)/
const SKIP_RE = /^(下属人数|工作地点|汇报对象|所属行业)[:：]?$|^\d+$/
const PROJECT_FIELD_RE = /^(项目职务|所在公司|项目描述|项目职责|项目业绩)[:：]?$/
const XSAAS_PROJECT_FIELD_RE = /^(责任描述|项目简介|内容)[:：]?$/
const BARE_PERIOD_RE = /^\d{4}[./年]\s*\d{1,2}(?:月)?\s*[-至]\s*(?:\d{4}[./年]\s*\d{1,2}(?:月)?|至今)$/
const EDUCATION_DEGREE_RE = /^(博士后|博士|硕士|本科|大专|中专|高中)$/
const EDUCATION_PERIOD_RE = /^(?:\d{4}[./年]\s*\d{1,2}(?:月)?\s*[-至]\s*(?:\d{4}[./年]\s*\d{1,2}(?:月)?|至今)|\d{4}\s*[-至]\s*(?:\d{4}|至今))$/
const SCHOOL_RE = /(?:大学|学院|学校|附中|University|College|School)$/i

export function parseWorkDetails(fullText: string): WorkDetailBlock[] {
  const text = String(fullText || '')
  const start = text.indexOf('工作经历')
  if (start < 0) return []
  const lines = text.slice(start + 4).split('\n').map(l => l.trim()).filter(Boolean)
  const blocks: WorkDetailBlock[] = []
  let current: WorkDetailBlock | null = null
  let inDuty = false
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (SECTION_END_RE.test(line)) break
    const isHeader = i + 1 < lines.length && PERIOD_RE.test(lines[i + 1])
    if (isHeader) {
      if (current) blocks.push(current)
      current = { company: line, period: '', role: '', description: [] }
      inDuty = false
      continue
    }
    if (!current) continue
    if (!current.period) {
      if (PERIOD_RE.test(line)) { current.period = line.replace(/[（）]/g, ''); continue }
      blocks.push(current)
      current = null
      continue
    }
    if (line === '职责业绩：' || line === '职责业绩:') { inDuty = true; continue }
    if (SKIP_RE.test(line)) continue
    if (!inDuty) { if (!line.includes('：')) current.role = line; continue }  // 行业/职位多行时职位取最后一行
    current.description.push(line)
  }
  if (current) blocks.push(current)
  return blocks.filter(b => b.description.length > 0)
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
  const tokens = String(educationText || '')
    .split('\n')
    .flatMap(line => line.split('·'))
    .map(token => token.trim())
    .filter(token => token && token !== '·')
  const blocks: EducationDetailBlock[] = []
  const pending: string[] = []
  let current: EducationDetailBlock | null = null
  const push = () => { if (current?.school) blocks.push(current) }
  const apply = (block: EducationDetailBlock, token: string) => {
    if (/^专业\s*\/\s*学历[:：]?$/.test(token)) return
    if (EDUCATION_PERIOD_RE.test(token)) { block.period = token; return }
    const degreeMatch = token.match(/^(.*)\s*\/\s*(博士后|博士|硕士|本科|大专|中专|高中)$/)
    if (degreeMatch) { block.major = degreeMatch[1].trim(); block.degree = degreeMatch[2]; return }
    if (EDUCATION_DEGREE_RE.test(token)) { block.degree = token; return }
    if (!block.major) { block.major = token; return }
    block.details.push(token)
  }
  for (const token of tokens) {
    if (SCHOOL_RE.test(token)) {
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
