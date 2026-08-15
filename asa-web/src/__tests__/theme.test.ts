import { beforeEach, describe, expect, it } from 'vitest'
import { applyTheme, readThemePreference, resolveTheme, setThemePreference } from '../agent/theme'

describe('theme preference', () => {
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.theme
  })

  it('defaults to auto and resolves through system preference (jsdom → light)', () => {
    expect(readThemePreference()).toBe('auto')
    expect(resolveTheme('auto')).toBe('light')
    expect(resolveTheme('light')).toBe('light')
    expect(resolveTheme('dark')).toBe('dark')
  })

  it('persists preference and applies <html data-theme>', () => {
    setThemePreference('dark')
    expect(localStorage.getItem('asaTheme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(readThemePreference()).toBe('dark')

    applyTheme('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem('asaTheme')).toBe('dark')
  })
})
