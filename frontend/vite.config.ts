import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'
import { createRequire } from 'node:module'
import tailwindcss from '@tailwindcss/postcss'
import autoprefixer from 'autoprefixer'

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
    // FAR-101 test configuration rationale (#817 + follow-up):
    //
    // 1. Timeouts (from #817): the suite (641 tests) includes heavy view specs
    //    whose mount+settle takes >5s under concurrent worker load on the CI
    //    runner (e.g. AdminRemyView pulls reka-ui + vue-query + many
    //    subcomponents). The default 5000ms testTimeout caused intermittent
    //    "Test timed out" flakes that are purely wall-clock, not assertion
    //    failures. 15000ms is generous but still bounded; fast tests complete
    //    in milliseconds.
    testTimeout: 15000,
    hookTimeout: 15000,
    // 2. Worker concurrency: the npm `test:unit` script hardcodes
    //    `--maxWorkers=4`, and CLI flags override anything set here — so the
    //    concurrency cap is deliberately NOT set in this file. It lives in
    //    `.github/workflows/ci.yml` (the Frontend and WCAG job), which invokes
    //    vitest directly with `--pool=threads --maxWorkers=2` to match the
    //    2-vCPU runner's actual capacity instead of over-subscribing it.
    // 3. New tests: prefer `vi.waitFor(() => expect(...).toBe(...), { timeout })`
    //    over `await flushPromises()` chains, give async setImmediate waits an
    //    adequate timeout, and always tear down timers/mocks in afterEach.
    //    Never use fixed sleeps to make a test pass.
    // 4. CSS injection: the default `css: false` stubs CSS imports, but the
    //    JsonViewer override-contract unit spec needs the real stylesheets
    //    (`json-viewer.css` + `vue-json-pretty`'s styles.css) mounted as
    //    <style> tags so it can enumerate cssRules. Only these two sheets are
    //    injected; all other CSS imports in the suite remain stubbed.
    css: {
      include: [/json-viewer/, /vue-json-pretty/],
    },
  },
  css: {
    postcss: {
      plugins: [tailwindcss(), autoprefixer()],
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
