const RELOAD_GUARD_KEY = 'asaChunkRecoverReloadAt'
const RELOAD_GUARD_MS = 30_000

/**
 * Vite 懒加载恢复：dist 重建后旧页面持有的 chunk 哈希失效，`import()` 抛
 * vite:preloadError。监听该事件并自动刷新一次换新资源，避免整个应用白屏；
 * sessionStorage 时间戳防止刷新后仍失败时的死循环。
 */
export function installChunkLoadRecovery(target: Window = window): void {
  target.addEventListener('vite:preloadError', event => {
    event.preventDefault() // 阻止未捕获的 import  rejection 继续传播
    const lastReload = Number(target.sessionStorage.getItem(RELOAD_GUARD_KEY) || 0)
    if (Date.now() - lastReload < RELOAD_GUARD_MS) return
    target.sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()))
    target.location.reload()
  })
}
