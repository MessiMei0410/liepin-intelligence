// 前端版本新鲜度：WKWebView 壳常驻运行，dist 一天内可能多次重建，正在运行的
// 旧 bundle 不会因 chunk 404 暴露（那是 chunkLoadRecovery 的场景），只能靠主动
// 比对构建指纹发现。当前 bundle 指纹由 vite define 注入；服务端 dist 指纹由
// Core 的 /api/v1/app-version 从 dist/build.json 读出。

// vitest 不经过 vite.config.ts 的 define，用 typeof 兜底为固定值，测试据此断言。
export const CURRENT_BUILD_ID: string = typeof __ASA_BUILD_ID__ === 'string' ? __ASA_BUILD_ID__ : 'test-build'

const DISMISS_KEY = 'asaVersionDismissedBuild'
export const VERSION_POLL_MS = 60_000

/** 本次会话内已忽略的新版本号：dismiss 后同版本不再提示，再出现更新版本时才重弹。 */
export const dismissedBuildId = (storage: Storage = sessionStorage): string => {
  try {
    return storage.getItem(DISMISS_KEY) || ''
  } catch {
    return ''
  }
}

export const dismissBuildId = (buildId: string, storage: Storage = sessionStorage): void => {
  try {
    storage.setItem(DISMISS_KEY, buildId)
  } catch {
    // 存储不可用（隐私模式等）时退化为仅本次内存态 dismiss。
  }
}

export type AppVersionResult = { ok?: boolean; build_id?: string | null }

/**
 * 读取服务端当前 dist 的构建指纹。返回 null 表示「无信息」——Core 不可达、
 * dist 未构建（503）或旧 dist 没有 build.json——这些场景一律不提示，避免误报。
 */
export const fetchServerBuildId = async (
  fetcher: typeof fetch = fetch,
): Promise<string | null> => {
  try {
    const response = await fetcher('/api/v1/app-version')
    if (!response.ok) return null
    const body = (await response.json()) as AppVersionResult
    return typeof body.build_id === 'string' && body.build_id ? body.build_id : null
  } catch {
    return null
  }
}

/** 判定是否需要提示：有新指纹、与运行中 bundle 不同、且本次会话未被忽略。 */
export const pendingUpdateBuildId = (serverBuildId: string | null, storage?: Storage): string => {
  if (!serverBuildId || serverBuildId === CURRENT_BUILD_ID) return ''
  if (serverBuildId === dismissedBuildId(storage)) return ''
  return serverBuildId
}
