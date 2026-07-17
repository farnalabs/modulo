import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

function yamlPlugin() {
  const yaml = require('js-yaml')
  return {
    name: 'vite-plugin-yaml',
    transform(code: string, id: string) {
      if (!id.endsWith('.yaml') && !id.endsWith('.yml')) return
      const parsed = yaml.load(code)
      const exported = JSON.stringify(parsed)
      return {
        code: `export default ${exported}`,
        map: null,
      }
    },
  }
}

export default defineConfig({
  plugins: [
    vue(),
    yamlPlugin(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    exclude: ['tests/e2e/**', 'node_modules/**'],
    setupFiles: ['./src/__tests__/setup.ts'],
  },
  css: {
    postcss: {
      plugins: {
        tailwindcss: {},
        autoprefixer: {},
      },
    },
  },
  build: {
    rolldownOptions: {
      checks: { pluginTimings: false },
    },
  },
  optimizeDeps: {
    exclude: ['vue-i18n'],
  },
  server: {
    port: 5173,
    allowedHosts: ['local-frontend.modulo.run', 'local.modulo.run'],
    proxy: {
      '/api': process.env.VITE_API_URL || 'http://localhost:8000',
      '/ws': { target: process.env.VITE_API_URL?.replace('http', 'ws') || 'ws://localhost:8000', ws: true },
    },
  },
})
