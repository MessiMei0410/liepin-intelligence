import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'ASA_')
  return {
    plugins: [react()],
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
