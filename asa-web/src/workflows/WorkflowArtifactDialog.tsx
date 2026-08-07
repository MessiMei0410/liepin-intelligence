import { useEffect, useMemo, useState } from 'react'
import { Download, FileText, LoaderCircle, TriangleAlert, X } from 'lucide-react'
import { AgentMessageContent } from '../agent/AgentMessageContent'
import { api, type WorkflowArtifactDetail } from '../api'
import { humanizeActionError } from '../shared/errors'
import { artifactStatusLabel, artifactTypeLabel } from './artifactPresentation'
import { useDialogFocus } from '../shared/useDialogFocus'

const formattedJson = (content: string, mimeType: string): string | null => {
  const text = content.trim()
  if (mimeType !== 'application/json' && !text.startsWith('{') && !text.startsWith('[')) return null
  try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return null }
}

export function WorkflowArtifactDialog({ artifactId, onClose }: { artifactId: string; onClose: () => void }) {
  const [artifact, setArtifact] = useState<WorkflowArtifactDetail | null>(null)
  const [error, setError] = useState('')
  const dialogRef = useDialogFocus<HTMLElement>(true)

  useEffect(() => {
    let alive = true
    api.workflowArtifact(artifactId)
      .then(payload => { if (alive) setArtifact(payload.artifact) })
      .catch(reason => { if (alive) setError(humanizeActionError(reason, '产物读取失败，请重试。')) })
    return () => { alive = false }
  }, [artifactId])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const jsonContent = useMemo(
    () => artifact ? formattedJson(artifact.content, artifact.mime_type) : null,
    [artifact],
  )

  return <div className="action-dialog-backdrop artifact-dialog-backdrop" role="presentation" onClick={onClose}>
    <section ref={dialogRef} className="action-dialog artifact-dialog" role="dialog" aria-modal="true" aria-labelledby="artifact-dialog-title" onClick={event => event.stopPropagation()}>
      <header>
        <span className="action-dialog-icon"><FileText /></span>
        <div>
          <small>{artifact ? `${artifactTypeLabel(artifact.artifact_type)} · ${artifactStatusLabel(artifact.validation_status)}` : '执行产物'}</small>
          <h3 id="artifact-dialog-title">{artifact?.title || '正在读取产物'}</h3>
        </div>
        <button className="icon-btn" onClick={onClose} title="关闭" aria-label="关闭"><X /></button>
      </header>
      <div className="action-dialog-body artifact-dialog-body">
        {!artifact && !error && <div className="artifact-loading" role="status"><LoaderCircle className="spin" /><span>正在读取产物…</span></div>}
        {error && <div className="artifact-error" role="alert"><TriangleAlert /><span>{error}</span></div>}
        {artifact && artifact.content_truncated && <div className="artifact-notice">正文较长，当前展示前 200,000 个字符；下载可查看完整内容。</div>}
        {artifact && jsonContent && <pre className="artifact-code">{jsonContent}</pre>}
        {artifact && !jsonContent && artifact.content && <AgentMessageContent content={artifact.content} />}
        {artifact && !artifact.content && <div className="artifact-file-only"><FileText /><b>该产物为文件</b><span>请下载后使用对应应用查看完整内容。</span></div>}
      </div>
      <footer>
        <button className="button" onClick={onClose}>关闭</button>
        {artifact?.downloadable && <a className="button primary" href={artifact.download_url} download={artifact.file_name}><Download />下载完整产物</a>}
      </footer>
    </section>
  </div>
}
