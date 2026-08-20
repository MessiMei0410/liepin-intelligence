import { useCallback, useEffect, useState } from 'react'
import { dismissBuildId, fetchServerBuildId, pendingUpdateBuildId, VERSION_POLL_MS } from './versionFreshness'

export type VersionFreshness = {
  /** 检测到的新版本构建指纹；空串表示无需提示。 */
  updateBuildId: string
  /** 本次会话内忽略该版本（换更新的版本会再次提示）。 */
  dismiss: () => void
}

/**
 * 版本新鲜度轮询：每 60s + 窗口回前台（visibilitychange/focus）时比对一次。
 * 只做检测不刷新——流式输出/表单输入中强刷会打断用户，何时刷由提示条按钮决定。
 */
export function useVersionFreshness(pollMs: number = VERSION_POLL_MS): VersionFreshness {
  const [updateBuildId, setUpdateBuildId] = useState('')
  useEffect(() => {
    let active = true
    let checking = false
    const check = async () => {
      if (checking) return
      checking = true
      try {
        const latest = await fetchServerBuildId()
        if (!active) return
        // 已有提示在展示时仍更新指纹（部署可能连续发生，提示始终指向最新 dist）。
        const pending = pendingUpdateBuildId(latest)
        if (pending) setUpdateBuildId(pending)
      } finally {
        checking = false
      }
    }
    const onForeground = () => {
      if (!document.hidden) void check()
    }
    void check()
    const timer = window.setInterval(() => {
      if (!document.hidden) void check()
    }, pollMs)
    document.addEventListener('visibilitychange', onForeground)
    window.addEventListener('focus', onForeground)
    return () => {
      active = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onForeground)
      window.removeEventListener('focus', onForeground)
    }
  }, [pollMs])
  const dismiss = useCallback(() => {
    setUpdateBuildId(current => {
      if (current) dismissBuildId(current)
      return ''
    })
  }, [])
  return { updateBuildId, dismiss }
}
