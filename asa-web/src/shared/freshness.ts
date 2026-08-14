/**
 * 共享的"新鲜度/过时"判定工具：统一时间戳解析与 stale 比较口径，
 * 供推荐包（panels/recommendationPackageFreshness）与策略复盘
 * （workflows/strategyReviewFreshness）复用，避免两处重复实现漂移。
 */

export const parseTimestamp = (value?: string): number | undefined => {
  const text = String(value || '').trim()
  if (!text) return undefined
  const parsed = Date.parse(text.includes('T') ? text : text.replace(' ', 'T'))
  return Number.isFinite(parsed) ? parsed : undefined
}

/**
 * 当 originalAt 与 updatedAt 都存在、且 updatedAt 晚于 originalAt 时判定为 stale。
 * 任一缺失或解析失败均不判定为 stale。
 */
export const isStaleAgainst = (originalAt?: string, updatedAt?: string): boolean => {
  const original = parseTimestamp(originalAt)
  const updated = parseTimestamp(updatedAt)
  return original !== undefined && updated !== undefined && updated > original
}
