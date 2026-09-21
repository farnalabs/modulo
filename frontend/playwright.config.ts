import { defineConfig } from '@playwright/test'
import { getTarget, getBaseUrl } from './tests/e2e/setup/env'

const coverageEnabled = process.env.VITE_COVERAGE === 'true' || process.env.npm_lifecycle_event === 'test:e2e:coverage'
const target = getTarget()
const noServer = (process.env.E2E_NO_WEBSERVER || '').toLowerCase() === 'true'

const baseURL = getBaseUrl(target)

export default defineConfig({
  testDir: './tests/e2e',
  retries: target !== 'local' ? 2 : 0,
  timeout: target !== 'local' ? 180_000 : 30_000,
  // Suite-level budget: comfortably above a healthy full run (~30-40 min for
  // staging workers:1) but FAR below the CI step's 90-min timeout. Catches
  // login-path regressions (FAR-1123) in minutes instead of burning the full
  // step budget on selector timeouts.
  globalTimeout: target !== 'local' ? 3_600_000 : undefined,
  workers: target === 'staging' ? 1 : undefined,
  use: {
    baseURL,
    trace: 'on-first-retry',  // capture trace on first retry for debugging
    screenshot: 'only-on-failure',
  },
  webServer: !noServer && target === 'local' ? {
    command: 'npm run dev -- --host 127.0.0.1',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
  } : undefined,
  globalSetup: require.resolve('./tests/e2e/setup/global-setup.ts'),
  globalTeardown: coverageEnabled ? './tests/e2e/setup/coverage-teardown.ts' : undefined,
})
