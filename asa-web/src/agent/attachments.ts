export const AGENT_ATTACHMENT_ACCEPT = '.xlsx,.xls,.csv,.docx,.pdf,.pptx,.txt,.md'
export const AGENT_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
export const AGENT_ATTACHMENT_MAX_COUNT = 3

const supportedExtensions = new Set(AGENT_ATTACHMENT_ACCEPT.split(','))

export type UploadedAgentAttachment = {
  attachment_id: string
  access_token: string
  file_name: string
  file_type: string
  mime_type: string
  size_bytes: number
  content_available: boolean
  truncated: boolean
  is_image: boolean
  status: string
}

export type QueuedAgentAttachment = {
  key: string
  fileName: string
  sizeBytes: number
  state: 'uploading' | 'ready' | 'error'
  attachment?: UploadedAgentAttachment
  error?: string
}

const uploadedAttachmentSchema = z.object({
  attachment_id: z.string().min(1),
  access_token: z.string().min(20),
  file_name: z.string().min(1),
  file_type: z.string(),
  mime_type: z.string(),
  size_bytes: z.number().int().nonnegative(),
  content_available: z.boolean(),
  truncated: z.boolean(),
  is_image: z.boolean(),
  status: z.string(),
})

export const formatAttachmentSize = (size: number) => {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.ceil(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export const validateAgentAttachment = (file: File): string => {
  const extension = `.${file.name.split('.').pop()?.toLowerCase() || ''}`
  if (!supportedExtensions.has(extension)) return `暂不支持 ${extension === '.' ? '无扩展名' : extension} 文件`
  if (!file.size) return '文件内容为空'
  if (file.size > AGENT_ATTACHMENT_MAX_BYTES) return '文件超过 25 MB 上限'
  return ''
}

const fileToBase64 = (file: File): Promise<string> => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onerror = () => reject(new Error('读取本地文件失败'))
  reader.onload = () => {
    const value = String(reader.result || '')
    const separator = value.indexOf(',')
    if (separator < 0) reject(new Error('本地文件编码失败'))
    else resolve(value.slice(separator + 1))
  }
  reader.readAsDataURL(file)
})

export async function uploadAgentAttachment(file: File): Promise<UploadedAgentAttachment> {
  const validationError = validateAgentAttachment(file)
  if (validationError) throw new Error(validationError)
  const response = await fetch('/api/v1/copilot/attachments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      request_id: `attachment_${crypto.randomUUID()}`,
      file_name: file.name,
      mime_type: file.type,
      size_bytes: file.size,
      content_base64: await fileToBase64(file),
    }),
  })
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) throw new Error(String(payload.detail || payload.error || `附件读取失败 (${response.status})`))
  const attachment = payload.attachment
  if (!attachment || typeof attachment !== 'object') throw new Error('附件读取结果不完整')
  const parsed = uploadedAttachmentSchema.safeParse(attachment)
  if (!parsed.success) throw new Error('附件读取结果与约定格式不一致')
  if (!parsed.data.content_available) throw new Error(parsed.data.status || '文件没有提取到可分析文本')
  return parsed.data
}
import { z } from 'zod'
