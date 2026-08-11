import { useEffect, useMemo, useState } from 'react'
import { Download, FileText, LoaderCircle, TriangleAlert } from 'lucide-react'
import { AgentMessageContent } from '../agent/AgentMessageContent'
import { api, type WorkflowArtifactDetail } from '../api'
import { humanizeActionError } from '../shared/errors'
import { artifactStatusLabel, artifactTypeLabel } from './artifactPresentation'
import { DialogModal } from '../shared/Dialog'

const formattedJson = (content: string, mimeType: string): string | null => {
  const text = content.trim()
  if (mimeType !== 'application/json' && !text.startsWith('{') && !text.startsWith('[')) return null
  try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return null }
}

export function WorkflowArtifactDialog({ artifactId, onClose }: { artifactId: string; onClose: () => void }) {
  const [artifact, setArtifact] = useState<WorkflowArtifactDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    api.workflowArtifact(artifactId)
      .then(payload => { if (alive) setArtifact(payload.artifact) })
      .catch(reason => { if (alive) setError(humanizeActionError(reason, '产物读取失败，请重试。')) })
    return () => { alive = false }
  }, [artifactId])

  const jsonContent = useMemo(
    () => artifact ? formattedJson(artifact.content, artifact.mime_type) : null,
    [artifact],
  )

  return <DialogModal
    onClose={onClose}
    title={artifact?.title || '正在读取产物'}
    titleId="artifact-dialog-title"
    icon={<FileText />}
    eyebrow={artifact ? `${artifactTypeLabel(artifact.artifact_type)} · ${artifactStatusLabel(artifact.validation_status)}` : '执行产物'}
    className="artifact-dialog"
    backdropClassName="artifact-dialog-backdrop"
    bodyClassName="artifact-dialog-body"
    footer={<>
      <button className="button" onClick={onClose}>关闭</button>
      {artifact?.downloadable && <a className="button primary" href={artifact.download_url} download={artifact.file_name}><Download />下载完整产物</a>}
    </>}
  >
    {!artifact && !error && <div className="artifact-loading" role="status"><LoaderCircle className="spin" /><span>正在读取产物…</span></div>}
    {error && <div className="artifact-error" role="alert"><TriangleAlert /><span>{error}</span></div>}
    {artifact && artifact.content_truncated && <div className="artifact-notice">正文较长，当前展示前 200,000 个字符；下载可查看完整内容。</div>}
    {artifact && jsonContent && <pre className="artifact-code">{jsonContent}</pre>}
    {artifact && !jsonContent && artifact.content && <AgentMessageContent content={artifact.content} />}
    {artifact && !artifact.content && <div className="artifact-file-only"><FileText /><b>该产物为文件</b><span>请下载后使用对应应用查看完整内容。</span></div>}
  </DialogModal>
}
