import vue from '@vitejs/plugin-vue'
import VueI18nPlugin from '@intlify/unplugin-vue-i18n/vite'
import { defineConfig } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'
import { resolve, dirname } from 'node:path'
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
    VueI18nPlugin({
      include: [resolve(dirname(fileURLToPath(import.meta.url)), './src/locales/*.js')],
    }),
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
  server: {
    port: 5173,
    proxy: {
      '/api': process.env.VITE_API_URL || 'http://localhost:8000',
      '/ws': { target: process.env.VITE_API_URL?.replace('http', 'ws') || 'ws://localhost:8000', ws: true },
    },
  },
})
