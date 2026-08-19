import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ErrorBoundary, isChunkLoadError } from '../shared/ErrorBoundary'
import { installChunkLoadRecovery } from '../shared/chunkLoadRecovery'

const Thrower = ({ error }: { error: Error }): never => {
  throw error
}

// 业务背景（dogfood P0-1）：名单弹窗点候选人后整页白屏（root 清空）——
// 懒加载面板/render 异常没有任何错误边界，React 直接卸载整棵树。
describe('ErrorBoundary', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('无异常时正常渲染子树', () => {
    render(<ErrorBoundary><p>正常内容</p></ErrorBoundary>)
    expect(screen.getByText('正常内容')).toBeInTheDocument()
  })

  it('渲染异常时显示可恢复兜底而不是清空 root', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ErrorBoundary label="候选人详情"><Thrower error={new Error('boom')} /></ErrorBoundary>)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('候选人详情出错了')
    expect(alert).toHaveTextContent('boom')
    expect(screen.getByRole('button', { name: /重试/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /刷新页面/ })).toBeInTheDocument()
  })

  it('chunk 加载失败（部署竞态）只引导刷新页面', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ErrorBoundary><Thrower error={new TypeError('Failed to fetch dynamically imported module: /asa-app/assets/CandidatePanel-abc123.js')} /></ErrorBoundary>)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('页面资源已更新')
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /刷新页面/ })).toBeInTheDocument()
  })

  it('重试按钮复位边界并重新渲染子树', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    let shouldThrow = true
    const Flaky = () => {
      if (shouldThrow) throw new Error('transient')
      return <p>恢复后的内容</p>
    }
    render(<ErrorBoundary><Flaky /></ErrorBoundary>)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    shouldThrow = false
    fireEvent.click(screen.getByRole('button', { name: /重试/ }))
    expect(screen.getByText('恢复后的内容')).toBeInTheDocument()
  })

  it('isChunkLoadError 识别各类懒加载失败特征', () => {
    expect(isChunkLoadError(new TypeError('Failed to fetch dynamically imported module'))).toBe(true)
    expect(isChunkLoadError(new Error('Loading chunk 123 failed'))).toBe(true)
    expect(isChunkLoadError(new Error('vite:preloadError'))).toBe(true)
    expect(isChunkLoadError(new Error('cannot read properties of undefined'))).toBe(false)
  })
})

describe('installChunkLoadRecovery', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    window.sessionStorage.clear()
  })

  it('vite:preloadError 触发一次自动刷新并阻止错误传播', () => {
    const reload = vi.fn()
    vi.stubGlobal('sessionStorage', window.sessionStorage)
    installChunkLoadRecovery(window)
    // jsdom 的 location.reload 未实现，直接替换不可行；用事件监听验证 preventDefault，
    // reload 通过 stub location 验证。
    const originalLocation = window.location
    // @ts-expect-error 测试环境替换 location
    delete window.location
    // @ts-expect-error 测试环境替换 location
    window.location = { ...originalLocation, reload }
    const event = new Event('vite:preloadError', { cancelable: true })
    window.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(reload).toHaveBeenCalledTimes(1)
    // 30s 守卫窗口内重复触发不再刷新（防死循环）
    window.dispatchEvent(new Event('vite:preloadError', { cancelable: true }))
    expect(reload).toHaveBeenCalledTimes(1)
    // @ts-expect-error 恢复 location
    window.location = originalLocation
  })
})
