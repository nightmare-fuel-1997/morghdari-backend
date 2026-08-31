import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Django demo routes serve /demo/ from demo-ui/dist — build there so
// Django's existing static serve picks it up with zero config changes.
export default defineConfig({
  plugins: [react()],
  base: '/demo/',
  build: {
    outDir: '../demo-ui/dist',
    emptyOutDir: true,
  },
  // Dev-server proxy: the API and demo endpoints live on Django (port 8000).
  // Without this, /api/* from the Vite origin 404s and login fails.
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/demo': {
        target: 'http://127.0.0.1:8000',
        // Only /demo/api/* goes to Django; everything else under /demo/
        // (the SPA and Vite module URLs) is served by Vite itself.
        bypass: (req) => {
          if (req.url && req.url.startsWith('/demo/api')) return undefined
          return req.url
        },
      },
    },
  },
})
