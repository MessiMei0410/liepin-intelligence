import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { sanitizeAgentVisibleText } from './agentTextSanitize'

// memo：长会话中追加消息时跳过已渲染消息的 markdown 重解析。
// 渲染层兜底脱敏：正文里的内部工具名（asa_*）替换为中文动作名（历史回填消息同样生效）。
export const AgentMessageContent = memo(function AgentMessageContent({ content }: { content: string }) {
  return <div className="agent-markdown">
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{sanitizeAgentVisibleText(content)}</ReactMarkdown>
  </div>
})

export function AgentThinking({ label }: { label: string }) {
  return <div className="agent-thinking" role="status" aria-label={label}>
    <span className="agent-thinking-dots" aria-hidden="true"><i/><i/><i/></span>
    <span>{label}</span>
  </div>
}

// DSH 思考过程折叠区（reasoning 流）：流式时强制展开并随增量滚到底，轮末自动收起、
// 可手动再展开。正文 markdown 重渲染不走这里（纯文本 pre-wrap，无解析开销）。
export function AgentThinkingBlock({ thinking, streaming }: { thinking: string; streaming?: boolean }) {
  return <details className={`agent-thinking-block ${streaming ? 'streaming' : ''}`} open={streaming || undefined}>
    <summary>{streaming ? '思考中…' : '思考过程'}</summary>
    <div className="agent-thinking-body" ref={el => { if (el && streaming) el.scrollTop = el.scrollHeight }}>{thinking}</div>
  </details>
}
