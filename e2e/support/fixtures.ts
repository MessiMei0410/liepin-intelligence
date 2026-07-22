import { test as base, expect } from '@playwright/test'

// 全局护栏：任何 JS 原生对话框（alert/confirm/prompt/beforeunload）出现即判失败。
// WKWebView 不实现 JS 对话框代理，仓内约定一律用 React 内对话框。
export const test = base.extend<{ nativeDialogGuard: void }>({
  nativeDialogGuard: [
    async ({ page }, use) => {
      const seen: string[] = []
      page.on('dialog', (dialog) => {
        seen.push(`${dialog.type()}: ${dialog.message()}`)
        void dialog.dismiss()
      })
      await use()
      expect(seen, '页面禁止弹出 JS 原生对话框').toEqual([])
    },
    { auto: true },
  ],
})

export { expect }

// global-setup 在依赖缺失时置 ASA_E2E_SKIP（降级模式，参照 tests/test_xsaas_search_parser.py）；
// 该变量在 worker 进程 fork 前设置，测试进程可直接读取。
export const e2eSkipReason = typeof process !== 'undefined' ? process.env.ASA_E2E_SKIP || '' : ''

export const skipIfNoBackend = () => {
  test.skip(Boolean(e2eSkipReason), e2eSkipReason || '隔离 Core 不可用')
}
