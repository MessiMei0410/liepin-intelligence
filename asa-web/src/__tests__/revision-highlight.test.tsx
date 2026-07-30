import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { parseRevisionHighlights } from '../workflows/revisionHighlight'
import { WorkflowStrategy } from '../workflows/WorkflowStrategy'

// 二期（PRD §5.3）：Copilot 补丁落地后左侧策略面板新增项 3 秒高亮 ——
// 从 goal.context.revision_instruction 解析「」值，命中 query chip 加 flash-new。

describe('parseRevisionHighlights', () => {
  it('从 revision_instruction 提取「」内文本并去重', () => {
    const context = {
      revision_number: 2,
      revision_instruction: '用户从 Copilot 建议中采纳了 2 项：新增场景词「SI/PI 仿真」；新增公司「中兴微电子」；新增公司「中兴微电子」',
    }
    expect(parseRevisionHighlights(context)).toEqual(['SI/PI 仿真', '中兴微电子'])
  })

  it('普通目标（revision 0）与无「」指令无高亮', () => {
    expect(parseRevisionHighlights({ revision_number: 0 })).toEqual([])
    expect(parseRevisionHighlights({ revision_number: 1, revision_instruction: '' })).toEqual([])
    expect(parseRevisionHighlights(null)).toEqual([])
    expect(parseRevisionHighlights(undefined)).toEqual([])
    expect(parseRevisionHighlights({ revision_number: 1, revision_instruction: '没有括号的指令' })).toEqual([])
  })

  it('过短/过长的「」值被过滤', () => {
    const long = '很'.repeat(41)
    expect(parseRevisionHighlights({ revision_number: 1, revision_instruction: `新增「x」和「${long}」` })).toEqual([])
  })
})

describe('WorkflowStrategy 新增项高亮', () => {
  const strategy = { generation: { mode: 'llm', model: 'ASA Model' } }
  const channels = {
    liepin: [
      { query: 'SI/PI 仿真 电源专家', purpose: '场景词' },
      { query: '电源专家', purpose: '岗位词' },
    ],
  }

  it('命中 highlights 的 query chip 带 flash-new class', () => {
    const { container } = render(
      <WorkflowStrategy strategy={strategy} channels={channels} gates={{}} highlights={['SI/PI 仿真']} open={false} toggle={() => undefined} />,
    )
    const chips = container.querySelectorAll('.strategy-queries>div')
    expect(chips[0].className).toContain('flash-new')
    expect(chips[1].className).not.toContain('flash-new')
  })

  it('不传 highlights 时无 flash-new', () => {
    const { container } = render(
      <WorkflowStrategy strategy={strategy} channels={channels} gates={{}} open={false} toggle={() => undefined} />,
    )
    expect(container.querySelector('.flash-new')).toBeNull()
  })
})
