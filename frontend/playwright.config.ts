import { defineConfig } from '@playwright/test'

const coverageEnabled = process.env.VITE_COVERAGE === 'true' || process.env.npm_lifecycle_event === 'test:e2e:coverage'
const target = (process.env.E2E_TARGET || 'local').toLowerCase()
const noServer = process.env.E2E_NO_WEBSERVER === 'true'

const BASE_URLS: Record<string, string> = {
  local: 'http://127.0.0.1:5173',
  staging: 'https://staging.modulo.run',
  app: 'https://app.modulo.run',
}

const baseURL = process.env.E2E_BASE_URL || BASE_URLS[target] || BASE_URLS.local

export default defineConfig({
  testDir: './tests/e2e',
  use: {
    baseURL,
  },
  webServer: !noServer && target === 'local' ? {
    command: 'npm run dev -- --host 127.0.0.1',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
  } : undefined,
  globalTeardown: coverageEnabled ? './tests/e2e/setup/coverage-teardown.ts' : undefined,
})
