// 主题偏好：auto（跟随系统）/ light / dark，持久化到 localStorage，驱动 <html data-theme>。
// CSS 只认 [data-theme="dark"] 一个入口；auto 由本模块在运行时解析成 light/dark，
// 并监听系统偏好变化，避免在样式里同时维护 @media 与属性选择器两份暗色层。

export type ThemeMode = 'auto' | 'light' | 'dark'

const STORAGE_KEY = 'asaTheme'

export const readThemePreference = (): ThemeMode => {
  const value = localStorage.getItem(STORAGE_KEY)
  return value === 'light' || value === 'dark' ? value : 'auto'
}

const systemDark = (): boolean => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false

export const resolveTheme = (mode: ThemeMode): 'light' | 'dark' => (
  mode === 'auto' ? (systemDark() ? 'dark' : 'light') : mode
)

export const applyTheme = (mode: ThemeMode): void => {
  document.documentElement.dataset.theme = resolveTheme(mode)
}

export const setThemePreference = (mode: ThemeMode): void => {
  localStorage.setItem(STORAGE_KEY, mode)
  applyTheme(mode)
}

// 进入即按偏好上色；auto 模式下跟随系统深浅切换。
export const initTheme = (): void => {
  applyTheme(readThemePreference())
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  mq?.addEventListener?.('change', () => {
    if (readThemePreference() === 'auto') applyTheme('auto')
  })
}
