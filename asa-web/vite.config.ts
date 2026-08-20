import { execSync } from 'node:child_process'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// 构建指纹：WKWebView 壳内的旧 bundle 以此比对 Core 当前伺服的 dist，
// 不一致时提示「新版本已就绪」。git sha 定位代码版本，时间戳保证同 commit
// 重复部署也会产生新指纹；ASA_BUILD_ID 可显式覆盖（CI/排查用）。
const gitSha = (() => {
  try {
    return execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return 'nogit'
  }
})()
const buildId = process.env.ASA_BUILD_ID || `${gitSha}-${Date.now()}`

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'ASA_')
  return {
    define: { __ASA_BUILD_ID__: JSON.stringify(buildId) },
    plugins: [
      react(),
      {
        name: 'asa-build-id',
        // 同步落盘 dist/build.json：Core 的 /api/v1/app-version 读它回答当前 dist 指纹。
        generateBundle() {
          this.emitFile({ type: 'asset', fileName: 'build.json', source: `${JSON.stringify({ build_id: buildId })}\n` })
        },
      },
    ],
    build: {
      rolldownOptions: {
        output: {
          manualChunks: (id: string) => {
            if (id.includes('/node_modules/react/') || id.includes('/node_modules/react-dom/')) return 'vendor-react'
            if (id.includes('/node_modules/lucide-react/')) return 'vendor-icons'
            if (id.includes('/node_modules/react-markdown/') || id.includes('/node_modules/remark-gfm/')) return 'vendor-markdown'
            if (id.includes('/node_modules/zod/')) return 'vendor-validation'
            return undefined
          },
        },
      },
    },
    server: { proxy: { '/api': env.ASA_CORE_URL || 'http://127.0.0.1:8765' } },
  }
})
