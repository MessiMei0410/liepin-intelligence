import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentSubagentBlock, subagentBlockSummary } from '../agent/AgentSubagentBlock'
import type { AgentSubagentRun } from '../agent/transport'

const runs: AgentSubagentRun[] = [
  { id: 'run-1', label: '背调候选人甲', status: 'done', summary: '甲已核实，在职。' },
  { id: 'run-2', label: '背调候选人乙', status: 'running' },
  { id: 'run-3', label: '调研竞品岗位', status: 'failed' },
]

describe('subagentBlockSummary（折叠态一行摘要）', () => {
  it('按状态聚合成「3 个子代理：1 运行中 · 1 失败 · 1 完成」', () => {
    expect(subagentBlockSummary(runs)).toBe('3 个子代理：1 运行中 · 1 失败 · 1 完成')
  })

  it('全部完成时只有完成项', () => {
    expect(subagentBlockSummary([
      { id: 'a', label: '', status: 'done' },
      { id: 'b', label: '', status: 'done' },
    ])).toBe('2 个子代理：2 完成')
  })
})

describe('AgentSubagentBlock（DSH 子代理执行卡片）', () => {
  it('有运行中子代理时强制展开，列出描述/状态/摘要', () => {
    const { container } = render(<AgentSubagentBlock subagents={runs}/>)
    const details = container.querySelector('details.agent-subagent-block')
    expect(details).not.toBeNull()
    expect(details).toHaveAttribute('open')
    expect(details).toHaveClass('streaming')
    expect(screen.getByText('3 个子代理：1 运行中 · 1 失败 · 1 完成')).toBeInTheDocument()
    expect(screen.getByText('背调候选人甲')).toBeInTheDocument()
    expect(screen.getByText('甲已核实，在职。')).toBeInTheDocument()
    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(screen.getByText('失败')).toBeInTheDocument()
  })

  it('全部终态后轮末收起；空 label 兜底「子代理任务」', () => {
    const { container } = render(<AgentSubagentBlock subagents={[
      { id: 'a', label: '', status: 'done', summary: '完成。' },
      { id: 'b', label: '乙任务', status: 'stopped' },
    ]}/>)
    const details = container.querySelector('details.agent-subagent-block')
    expect(details).not.toHaveAttribute('open')
    expect(details).not.toHaveClass('streaming')
    expect(screen.getByText('2 个子代理：1 已停止 · 1 完成')).toBeInTheDocument()
    expect(screen.getByText('子代理任务')).toBeInTheDocument()
  })

  it('运行中→完成的状态更新反映到摘要与样式', () => {
    const { container, rerender } = render(<AgentSubagentBlock subagents={[{ id: 'a', label: '任务', status: 'running' }]}/>)
    expect(container.querySelector('details')).toHaveAttribute('open')
    rerender(<AgentSubagentBlock subagents={[{ id: 'a', label: '任务', status: 'done', summary: '搞定' }]}/>)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
    expect(screen.getByText('1 个子代理：1 完成')).toBeInTheDocument()
  })

  it('空数组不渲染', () => {
    const { container } = render(<AgentSubagentBlock subagents={[]}/>)
    expect(container.querySelector('details')).toBeNull()
  })
})
