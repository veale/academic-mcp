import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/webapp/',
  build: {
    outDir: '../webapp_dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/webapp/api': {
        target: 'http://localhost:8765',
        changeOrigin: true,
      },
    },
  },
})
