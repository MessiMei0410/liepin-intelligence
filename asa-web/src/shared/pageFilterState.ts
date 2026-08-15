import { Dispatch, SetStateAction, useCallback, useState } from 'react'

// 页面筛选/排序/分页状态的会话级持久化。
// 背景：App 对四个主 tab 做条件渲染，切 tab 即卸载页面组件，用户调好的
// 搜索词/范围/排序/页码全部丢失；刷新后同样重建。这里用 sessionStorage
// 按 key 保住「用户已经调好的视图」，切 tab 回来或刷新后不重置。
// 写入失败（隐私模式/配额）时静默退化为普通内存态，不阻断输入。
const PREFIX = 'asaPageFilter.'
const SCHEMA_KEY = `${PREFIX}version`
const SCHEMA_VERSION = '1'

const readStored = <T,>(key: string, fallback: T): T => {
  try {
    const raw = sessionStorage.getItem(PREFIX + key)
    if (raw === null) return fallback
    const parsed: unknown = JSON.parse(raw)
    return parsed === null || parsed === undefined ? fallback : (parsed as T)
  } catch {
    return fallback
  }
}

const writeStored = <T,>(key: string, value: T): void => {
  try {
    sessionStorage.setItem(PREFIX + key, JSON.stringify(value))
  } catch {
    // 存储不可用：保底内存态，不影响当前会话内的状态保持。
  }
}

/** 结构版本升级时整体失效，避免读到旧 schema 的残留值。 */
const ensureSchemaVersion = () => {
  try {
    if (sessionStorage.getItem(SCHEMA_KEY) === SCHEMA_VERSION) return
    const staleKeys: string[] = []
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index)
      if (key && key.startsWith(PREFIX) && key !== SCHEMA_KEY) staleKeys.push(key)
    }
    for (const key of staleKeys) sessionStorage.removeItem(key)
    sessionStorage.setItem(SCHEMA_KEY, SCHEMA_VERSION)
  } catch {
    // 忽略：退化为内存态。
  }
}

/**
 * 与 useState 同签名、但随 sessionStorage 持久的页面筛选状态。
 * 仅用于列表页的 UI 视图状态（搜索词/范围/排序/页码），不要存业务数据。
 */
export function usePageFilterState<T>(key: string, fallback: T): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    ensureSchemaVersion()
    return readStored(key, fallback)
  })
  const update = useCallback<Dispatch<SetStateAction<T>>>(next => {
    setValue(current => {
      const resolved = typeof next === 'function' ? (next as (previous: T) => T)(current) : next
      writeStored(key, resolved)
      return resolved
    })
  }, [key])
  return [value, update]
}
