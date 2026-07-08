import { defineConfig } from '@playwright/test'

const coverageEnabled = process.env.VITE_COVERAGE === 'true' || process.env.npm_lifecycle_event === 'test:e2e:coverage'

export default defineConfig({
  testDir: './tests/e2e',
  use: {
    baseURL: 'http://127.0.0.1:5173',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
  },
  globalTeardown: coverageEnabled ? './tests/e2e/setup/coverage-teardown.ts' : undefined,
})
