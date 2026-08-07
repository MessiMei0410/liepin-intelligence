import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function AgentMessageContent({ content }: { content: string }) {
  return <div className="agent-markdown">
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{content}</ReactMarkdown>
  </div>
}

export function AgentThinking({ label }: { label: string }) {
  return <div className="agent-thinking" role="status" aria-label={label}>
    <span className="agent-thinking-dots" aria-hidden="true"><i/><i/><i/></span>
    <span>{label}</span>
  </div>
}
