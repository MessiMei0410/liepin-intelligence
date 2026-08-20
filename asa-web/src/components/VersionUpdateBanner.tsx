import { RefreshCw, X } from 'lucide-react'
import { useVersionFreshness } from '../shared/useVersionFreshness'

// 新版本提示条：dist 已重建但本窗口仍跑旧 bundle 时，顶部细条提示，由用户
// 决定何时刷新（不自动强刷，避免打断流式输出/表单输入）；dismiss 后本次会话
// 同版本不再弹。固定定位，不挤压布局，浮窗窄屏下收缩为一行。
export function VersionUpdateBanner() {
  const { updateBuildId, dismiss } = useVersionFreshness()
  if (!updateBuildId) return null
  return (
    <div className="version-update-banner" role="status">
      <span className="version-update-text">新版本已就绪</span>
      <button className="version-update-reload" onClick={() => window.location.reload()}>
        <RefreshCw />点击刷新
      </button>
      <button className="version-update-dismiss" aria-label="本次忽略" onClick={dismiss}><X /></button>
    </div>
  )
}
