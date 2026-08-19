import { Component, type ErrorInfo, type ReactNode } from 'react'
import { RefreshCw, RotateCcw, ShieldAlert } from 'lucide-react'

/** 懒加载 chunk 失败（部署后旧页面拉不到新哈希的 chunk 文件）的典型报错特征。 */
export function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? `${error.name} ${error.message}` : String(error)
  return /Loading chunk|dynamically imported module|vite:preloadError|Failed to fetch/i.test(message)
}

type ErrorBoundaryProps = {
  children: ReactNode
  /** 出错时的区域说明（如「候选人详情」），缺省为整个应用。 */
  label?: string
  onError?: (error: Error, info: ErrorInfo) => void
}

type ErrorBoundaryState = { error?: Error }

/**
 * 全局兜底错误边界：任何渲染/懒加载异常不再导致 root 清空白屏，
 * 而是给出可恢复的兜底 UI。chunk 加载失败（部署竞态）只能刷新页面恢复
 * （React.lazy 会缓存 rejected import，原地重试无意义）；其余错误可原地重试。
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {}

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 保留控制台堆栈，便于 dogfood/排障时取证。
    console.error('[ErrorBoundary]', error, info.componentStack)
    this.props.onError?.(error, info)
  }

  private reset = () => this.setState({ error: undefined })

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    const chunkError = isChunkLoadError(error)
    return (
      <div className="empty error-boundary-fallback" role="alert">
        <ShieldAlert size={28} />
        <p><b>{this.props.label || '页面'}出错了{chunkError ? '：页面资源已更新' : ''}</b></p>
        <p>{chunkError
          ? '应用刚发布过新版本，当前页面的部分资源已过期，刷新后即可恢复。'
          : `渲染异常（${error.message || '未知错误'}）。可尝试重试，仍失败请刷新页面。`}</p>
        <div className="error-boundary-actions">
          {chunkError
            ? <button className="button primary" onClick={() => window.location.reload()}><RefreshCw size={14}/>刷新页面</button>
            : <>
                <button className="button primary" onClick={this.reset}><RotateCcw size={14}/>重试</button>
                <button className="button" onClick={() => window.location.reload()}><RefreshCw size={14}/>刷新页面</button>
              </>}
        </div>
      </div>
    )
  }
}
