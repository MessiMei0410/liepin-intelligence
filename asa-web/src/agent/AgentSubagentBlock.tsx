import type { AgentSubagentRun } from './transport'

const STATUS_LABEL: Record<AgentSubagentRun['status'], string> = {
  running: '运行中',
  done: '完成',
  failed: '失败',
  stopped: '已停止',
}

// 状态聚合顺序：运行中 > 失败 > 已停止 > 完成（折叠态摘要按此顺序拼接非零项）。
const STATUS_ORDER: AgentSubagentRun['status'][] = ['running', 'failed', 'stopped', 'done']

/** 折叠态一行摘要：「3 个子代理：2 完成 · 1 运行中」。 */
export function subagentBlockSummary(subagents: AgentSubagentRun[]): string {
  const counts = new Map<AgentSubagentRun['status'], number>()
  for (const run of subagents) counts.set(run.status, (counts.get(run.status) || 0) + 1)
  const parts = STATUS_ORDER.filter(status => counts.get(status)).map(status => `${counts.get(status)} ${STATUS_LABEL[status]}`)
  return `${subagents.length} 个子代理：${parts.join(' · ')}`
}

// DSH 子代理执行卡片（参照 AgentThinkingBlock 的 <details> 折叠形态）：折叠态一行摘要，
// 展开列出每个子代理的描述 + 状态 + 结果摘要。流式期（有 running）强制展开，轮末收起。
export function AgentSubagentBlock({ subagents, streaming }: { subagents: AgentSubagentRun[]; streaming?: boolean }) {
  if (!subagents.length) return null
  const hasRunning = subagents.some(run => run.status === 'running')
  const open = streaming || hasRunning || undefined
  return <details className={`agent-subagent-block ${hasRunning ? 'streaming' : ''}`} open={open}>
    <summary>{subagentBlockSummary(subagents)}</summary>
    <ul className="agent-subagent-list">
      {subagents.map(run => (
        <li key={run.id} className={`agent-subagent-item ${run.status}`}>
          <span className={`agent-subagent-status ${run.status}`}>{STATUS_LABEL[run.status]}</span>
          <span className="agent-subagent-label">{run.label || '子代理任务'}</span>
          {run.summary && <span className="agent-subagent-summary">{run.summary}</span>}
        </li>
      ))}
    </ul>
  </details>
}
