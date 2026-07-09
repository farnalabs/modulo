import { defineConfig } from '@playwright/test'
import { getTarget, getBaseUrl } from './tests/e2e/setup/env'

const coverageEnabled = process.env.VITE_COVERAGE === 'true' || process.env.npm_lifecycle_event === 'test:e2e:coverage'
const target = getTarget()
const noServer = (process.env.E2E_NO_WEBSERVER || '').toLowerCase() === 'true'

const baseURL = getBaseUrl(target)

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
  globalSetup: require.resolve('./tests/e2e/setup/global-setup.ts'),
  globalTeardown: coverageEnabled ? './tests/e2e/setup/coverage-teardown.ts' : undefined,
})
