import { describe, expect, it } from 'vitest'
import { plainTextPreview, sanitizeAgentVisibleText } from '../agent/agentTextSanitize'

describe('正文工具名脱敏（渲染层兜底）', () => {
  it('已映射的 asa_* 工具名替换为中文动作名（含反引号包裹形态）', () => {
    expect(sanitizeAgentVisibleText('在 `asa_pool_filter` 的严格分级输出里只给了计数')).toBe('在 名单筛选 的严格分级输出里只给了计数')
    expect(sanitizeAgentVisibleText('我用 asa_approvals 查到 2 条待审批')).toBe('我用 审批查询 查到 2 条待审批')
  })

  it('未映射的内部工具名统一脱敏为「内部工具」，普通文本不受影响', () => {
    expect(sanitizeAgentVisibleText('asa_future_tool 返回了 3 条')).toBe('内部工具 返回了 3 条')
    expect(sanitizeAgentVisibleText('张雯已触达，建议今天电话')).toBe('张雯已触达，建议今天电话')
  })
})

describe('任务栏摘要纯文本化', () => {
  it('剥离加粗/转义/表格管道符/标题/链接等 markdown 标记', () => {
    expect(plainTextPreview('**候选人**：陈义豪')).toBe('候选人：陈义豪')
    expect(plainTextPreview('俞\\*\\* 待核验')).toBe('俞** 待核验')
    expect(plainTextPreview('| 审批 ID | 岗位 |\n| --- | --- |\n| a1 | 电源专家 |')).toBe('审批 ID 岗位 a1 电源专家')
    expect(plainTextPreview('## 结论\n- 先看[名单](http://x) \n> 引用')).toBe('结论 先看名单 引用')
    expect(plainTextPreview('`asa_pool_filter` 输出了 15 人')).toBe('名单筛选 输出了 15 人')
  })

  it('折叠多余空白并去首尾空格', () => {
    expect(plainTextPreview('  第一行\n\n第二行  ')).toBe('第一行 第二行')
    expect(plainTextPreview('')).toBe('')
  })
})
