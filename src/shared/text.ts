export function copilotText(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(copilotText).filter(Boolean).join('\n')
  if (!value || typeof value !== 'object') return ''
  const item = value as Record<string, unknown>
  for (const key of ['text', 'content', 'answer', 'message', 'summary', 'title', 'label']) {
    const result = copilotText(item[key])
    if (result) return result
  }
  return ''
}
