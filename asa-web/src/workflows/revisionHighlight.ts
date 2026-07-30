// 二期高亮（PRD §5.3）：Copilot 补丁经修订链落地后，左侧策略面板把本次新增项闪 3 秒。
// 数据来源是 goal.context.revision_instruction 里的「新增…「值」」条款
// （patch 应用时由后端按 instruction_prefix/suffix 模板拼装）；
// RevisePlanDialog 的自由文本修订没有「」条款，自然降级不闪。
export function parseRevisionHighlights(context: unknown): string[] {
  const ctx = (context && typeof context === 'object' ? context : {}) as Record<string, unknown>
  if (Number(ctx.revision_number || 0) < 1) return []
  const instruction = String(ctx.revision_instruction || '')
  const values: string[] = []
  for (const match of instruction.matchAll(/「([^「」]{2,40})」/g)) {
    const value = match[1].trim()
    if (value && !values.includes(value)) values.push(value)
  }
  return values
}
