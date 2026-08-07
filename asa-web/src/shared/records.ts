export const recordValue = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
export const arrayValue = (value: unknown): unknown[] => Array.isArray(value) ? value : []
export const textList = (...values: unknown[]): string[] => [...new Set(values.flatMap(value => {
  if(Array.isArray(value))return value.map(String)
  const text=String(value||'').trim()
  if(!text)return []
  if(text.startsWith('[')){try{const parsed=JSON.parse(text);if(Array.isArray(parsed))return parsed.map(String)}catch{/* Keep text fallback. */}}
  return text.split(/[\n；;、]+/)
}).map(value=>value.trim()).filter(value=>Boolean(value)&&!/^[0-9]+$/.test(value)))]
