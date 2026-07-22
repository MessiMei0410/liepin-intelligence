import { defineConfig } from '@playwright/test'
import { E2E_BASE_URL } from './e2e/support/paths'

// R6 E2E：浏览器一律打隔离 Core（127.0.0.1:8876 + /tmp 新鲜 DB 副本），
// 由 global-setup 复制正式库（只读）并拉起实例，global-teardown 回收。
// /asa-app 仅对 ASAApp/ UA 开放，故 use.userAgent 固定带该前缀。
//
// 运行方式：
//   npm run test:e2e          — 先功能组（有写操作）再截图组（各自新鲜副本，互不污染基线）
//   npx playwright test --project=functional
//   npx playwright test --project=shots-desktop --project=shots-floating
export default defineConfig({
  testDir: './e2e',
  testMatch: /(functional|shots)\/.*\.spec\.ts/,
  timeout: 45_000,
  expect: {
    timeout: 10_000,
    // 本机同浏览器复跑应几乎零差异；容差仅覆盖字体抗锯齿抖动。
    toHaveScreenshot: { animations: 'disabled', threshold: 0.2, maxDiffPixelRatio: 0.01 },
  },
  retries: 0,
  workers: 3,
  reporter: [['list']],
  outputDir: 'work/playwright-results',
  snapshotDir: './e2e/snapshots',
  snapshotPathTemplate: '{snapshotDir}/{testFileName}/{arg}-{projectName}-{platform}{ext}',
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  use: {
    baseURL: E2E_BASE_URL,
    userAgent: 'ASAApp/1.0 (E2E; Playwright)',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'functional',
      testMatch: /functional\/.*\.spec\.ts/,
      use: { viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'shots-desktop',
      testMatch: /shots\/.*\.spec\.ts/,
      use: { viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'shots-floating',
      testMatch: /shots\/.*\.spec\.ts/,
      use: { viewport: { width: 390, height: 700 } },
    },
  ],
})
