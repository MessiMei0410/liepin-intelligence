// 面向用户的正文/摘要净化（渲染层兜底）。
// 第一道防线是模型护栏（dsh asa-profile AGENTS.md「表达规范」：正文禁内部工具名/裸 ID）；
// 这里兜底两件事：历史已回填消息里的内部工具名脱敏为中文动作名（与 dsh/asa-server
// TOOL_LABELS 同口径），任务栏摘要的裸 markdown 标记纯文本化（2026-08-19 dogfood：
// 摘要里 `**候选人**`、表格管道符外露）。

// asa_* 工具名 → 用户可懂的中文动作名（映射口径同 dsh/asa-server/lib/index.js TOOL_LABELS）。
const AGENT_TOOL_LABELS: Record<string, string> = {
  asa_dashboard: '工作台总览',
  asa_jobs: '岗位查询',
  asa_candidates: '候选人查询',
  asa_candidate_profile: '人选档案查询',
  asa_workflow: '工作流查询',
  asa_approvals: '审批查询',
  asa_pool_filter: '名单筛选',
  asa_candidate_list_card: '名单卡生成',
  asa_dedupe_scan: '重复人选扫描',
  asa_candidate_preflight: '候选人操作预检',
  asa_approval_preflight: '审批预检',
  asa_workflow_action_preflight: '工作流动作预检',
  asa_resume_backfill: '简历回填',
  asa_copilot_ask: '领域分析',
}

/** 正文脱敏：裸工具名（含反引号包裹形态）替换为中文动作名；未知名称统一作「内部工具」。 */
export const sanitizeAgentVisibleText = (text: string): string =>
  String(text || '').replace(/`?(asa_[a-z0-9_]+)`?/g, (_match, name: string) => AGENT_TOOL_LABELS[name] || '内部工具')

/** 任务栏摘要纯文本化：先去反斜杠转义与代码/链接/表格/强调等 markdown 标记，再折叠空白。 */
export const plainTextPreview = (text: string): string =>
  sanitizeAgentVisibleText(text)
    .replace(/\\([*_~`|[\]#>])/g, '$1')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    // 表格分隔行/水平线（只含 | - : 空白的整行）整体剔除，再删残余管道符。
    .replace(/^[|:\-\s]+$/gm, ' ')
    .replace(/\|/g, ' ')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/(\*|_)([^*_]+)\1/g, '$2')
    .replace(/~~(.*?)~~/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
