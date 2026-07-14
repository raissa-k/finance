import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': {
        // Defaults to the host-exposed backend port for `npm run dev` on the
        // host; the Docker dev service overrides this to the service name
        // (`backend`) since containers don't share localhost with the host.
        target: process.env.VITE_DEV_API_PROXY_TARGET || 'http://localhost:8000',
        changeOrigin: true,
        secure: false,
      }
    }
  }
})
