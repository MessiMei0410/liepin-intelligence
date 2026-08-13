/** macOS 宿主（WKWebView）原生桥：ASA 桌面端 AppDelegate 统一处理 asaNative 消息。 */
export function nativeBridge(type: string, payload: Record<string, unknown>): boolean {
  const handler = (window as unknown as {
    webkit?: { messageHandlers?: { asaNative?: { postMessage: (msg: unknown) => void } } }
  }).webkit?.messageHandlers?.asaNative
  if (!handler) return false
  handler.postMessage({ type, ...payload })
  return true
}

/** 独立窗口纯净模式：hash 带 bare=1 时只渲染目标页面（详情/名单），不带 Agent 主界面。 */
export function isBareDetached(): boolean {
  return new URLSearchParams(window.location.hash.slice(1)).get('bare') === '1'
}
