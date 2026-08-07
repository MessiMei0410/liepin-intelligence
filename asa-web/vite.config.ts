import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'ASA_')
  return {
    plugins: [react()],
    server: { proxy: { '/api': env.ASA_CORE_URL || 'http://127.0.0.1:8765' } },
  }
})
