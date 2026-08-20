import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VersionUpdateBanner } from '../components/VersionUpdateBanner'
import { CURRENT_BUILD_ID, dismissedBuildId, pendingUpdateBuildId } from '../shared/versionFreshness'
import { mockResponse } from './helpers'

// 版本新鲜度提示条：构建指纹一致不提示，不一致出现提示，dismiss 后同版本不再弹，
// 任何情况下都不自动刷新（流式/输入中由用户决定何时刷）。
const stubAppVersion = (buildId: string | null, ok = true) => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/app-version') {
      return Promise.resolve(mockResponse(ok ? { ok: true, build_id: buildId } : { ok: false, error: 'ASA Web 尚未构建' }, ok, ok ? 200 : 503))
    }
    return Promise.resolve(mockResponse({ ok: true }))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('版本新鲜度提示条', () => {
  const realLocation = window.location
  beforeEach(() => {
    sessionStorage.clear()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
  })

  it('构建指纹一致时不显示提示', async () => {
    stubAppVersion(CURRENT_BUILD_ID)
    render(<VersionUpdateBanner />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    // 轮询多轮后仍不提示
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('构建指纹不一致时出现提示，且绝不自动刷新', async () => {
    stubAppVersion('new-build-456')
    const reloadSpy = vi.fn()
    // jsdom 的 location.reload 未实现；替换成 spy 以断言从未被自动调用。
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload: reloadSpy },
    })
    render(<VersionUpdateBanner />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const banner = screen.getByRole('status')
    expect(banner).toHaveTextContent('新版本已就绪')

    // 模拟流式进行中长时间停留：多轮轮询过去，提示保持但从不自动刷新
    await act(async () => { await vi.advanceTimersByTimeAsync(300_000) })
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(reloadSpy).not.toHaveBeenCalled()

    // 用户点击刷新才触发 reload
    fireEvent.click(screen.getByRole('button', { name: /点击刷新/ }))
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it('dismiss 后本次会话同版本不再提示，更新的版本出现时再弹', async () => {
    let serverBuild: string | null = 'build-v2'
    const fetchMock = vi.fn(() => Promise.resolve(mockResponse({ ok: true, build_id: serverBuild })))
    vi.stubGlobal('fetch', fetchMock)
    render(<VersionUpdateBanner />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByRole('status')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '本次忽略' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(dismissedBuildId()).toBe('build-v2')

    // 后续轮询拿到同一版本：不再提示
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    // dist 再次重建出更新版本：重新提示
    serverBuild = 'build-v3'
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(screen.getByRole('status')).toHaveTextContent('新版本已就绪')
  })

  it('Core 不可达或 dist 未构建（503）时不提示', async () => {
    stubAppVersion(null, false)
    render(<VersionUpdateBanner />)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('窗口从后台回前台（visibilitychange）时立即比对', async () => {
    let serverBuild: string | null = CURRENT_BUILD_ID
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(mockResponse({ ok: true, build_id: serverBuild }))))
    render(<VersionUpdateBanner />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    // 后台期间 dist 重建，回到前台立即提示（不等下一轮 60s 轮询）
    serverBuild = 'build-while-hidden'
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await Promise.resolve()
    })
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('pendingUpdateBuildId 纯函数：无指纹/同版本/已忽略均不提示', () => {
    expect(pendingUpdateBuildId(null)).toBe('')
    expect(pendingUpdateBuildId(CURRENT_BUILD_ID)).toBe('')
    sessionStorage.setItem('asaVersionDismissedBuild', 'build-x')
    expect(pendingUpdateBuildId('build-x')).toBe('')
    expect(pendingUpdateBuildId('build-y')).toBe('build-y')
  })
})
