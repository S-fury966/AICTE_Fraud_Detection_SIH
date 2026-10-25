import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// The API serves on localhost:8765 (python -m app.api). Proxying /api keeps the
// dashboard same-origin, so evidence images load with a plain <img src>.
// Override with API_TARGET=http://127.0.0.1:<port> npm run dev.
const apiTarget = process.env.API_TARGET ?? 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [tailwindcss(), react()],
  server: {
    port: 5175,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
