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

const PERIOD_RE = /^（[^）]*(\d{4}|至今)[^）]*）$/
const SECTION_END_RE = /^(教育经历|项目经历|项目经验|技能|语言能力|自我评价|附件简历)/
const SKIP_RE = /^(下属人数|工作地点|汇报对象|所属行业)[:：]?$|^\d+$/

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
