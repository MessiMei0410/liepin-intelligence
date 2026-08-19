import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentThinkingBlock } from '../agent/AgentMessageContent'

describe('AgentThinkingBlock（DSH 思考过程折叠区）', () => {
  it('流式中：强制展开、标题「思考中…」、内容可见', () => {
    const { container } = render(<AgentThinkingBlock thinking="先分析岗位画像" streaming/>)
    const details = container.querySelector('details.agent-thinking-block')
    expect(details).not.toBeNull()
    expect(details).toHaveAttribute('open')
    expect(details).toHaveClass('streaming')
    expect(screen.getByText('思考中…')).toBeInTheDocument()
    expect(screen.getByText('先分析岗位画像')).toBeInTheDocument()
  })

  it('轮末：自动收起、标题「思考过程」、内容仍在 DOM（可手动展开）', () => {
    const { container } = render(<AgentThinkingBlock thinking="完整推理"/>)
    const details = container.querySelector('details.agent-thinking-block')
    expect(details).not.toBeNull()
    expect(details).not.toHaveAttribute('open')
    expect(details).not.toHaveClass('streaming')
    expect(screen.getByText('思考过程')).toBeInTheDocument()
    expect(screen.getByText('完整推理')).toBeInTheDocument()
  })

  it('流式结束切换 streaming：open 属性随渲染移除（自动收起）', () => {
    const { container, rerender } = render(<AgentThinkingBlock thinking="推理" streaming/>)
    expect(container.querySelector('details')).toHaveAttribute('open')
    rerender(<AgentThinkingBlock thinking="推理"/>)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
  })
})
