import { useEffect, useRef } from 'react'
import { nativeCopilotBridge, publishCopilotContext } from './bridge'

// R12-b：surface=copilot 降级为纯转发器——原生浮窗是唯一 Copilot 交互界面，
// 本页不再渲染任何对话 UI，也不调用 Copilot 写接口。
export function CopilotSurface() {
  const bridge = nativeCopilotBridge()
  const forwardedRef = useRef(false)
  useEffect(() => {
    if (!bridge || forwardedRef.current) return
    forwardedRef.current = true
    // 上下文只走服务端仲裁一条通道（通道 B/URL 投递已随 R12-b 拆除），这里发布默认页上下文后唤起浮窗。
    void publishCopilotContext({ type: 'page', page: 'overview' }, 'copilot', true).finally(() => {
      bridge.postMessage({ type: 'showFloating' })
      window.close()
    })
  }, [bridge])
  if (bridge) return null
  return (
    <main className="copilot-surface">
      <div className="copilot-forward-note">
        <b>请在 ASA App 中使用浮窗</b>
        <span>浏览器页面不再提供 Copilot 对话；请打开 ASA App，通过顶栏 Copilot 按钮唤起浮窗继续。</span>
      </div>
    </main>
  )
}
