import { defineConfig } from '@playwright/test'
import { getTarget, getBaseUrl, getLocalE2eBaseUrl, getLocalE2ePort } from './tests/e2e/setup/env'

const coverageEnabled = process.env.VITE_COVERAGE === 'true' || process.env.npm_lifecycle_event === 'test:e2e:coverage'
const target = getTarget()
const noServer = (process.env.E2E_NO_WEBSERVER || '').toLowerCase() === 'true'

const baseURL = getBaseUrl(target)

// The URL of the dev server Playwright starts for a local run. It is derived
// from the worktree path (5173 in CI, where the collision cannot happen) so
// concurrent worktrees never share a server — see getLocalE2ePort() in
// tests/e2e/setup/env.ts.
const localServerUrl = getLocalE2eBaseUrl()
const localServerPort = getLocalE2ePort()

export default defineConfig({
  testDir: './tests/e2e',
  retries: target !== 'local' ? 2 : 0,
  timeout: target !== 'local' ? 180_000 : 30_000,
  // Bound a cascade. The staging @regression job runs 265 tests serially, so a
  // systemic failure (e.g. a pre-auth/login degradation) otherwise retries
  // every remaining test 3× at the 180 s budget and exhausts the job's
  // 90-minute timeout before Playwright can report a signal — the deploy
  // pipeline then sees an opaque "exceeded maximum execution time" cancelled
  // run. Aborting after a clear failure count turns that into a fast, legible
  // failure and cannot fail an otherwise-healthy run (zero final failures).
  maxFailures: target !== 'local' ? 15 : undefined,
  workers: target === 'staging' ? 1 : undefined,
  use: {
    baseURL,
    trace: 'on-first-retry',  // capture trace on first retry for debugging
    screenshot: 'only-on-failure',
  },
  webServer: !noServer && target === 'local' ? {
    // FAR-1900: `--strictPort` makes Vite exit if the derived port is taken
    // (instead of quietly moving to port+1, which would leave Playwright
    // polling — and silently reusing — whatever foreign server held the
    // originally requested port), and `reuseExistingServer: false` stops
    // Playwright attaching to an already-running server. Together a local run
    // binds its own dev server or fails loudly at startup; it can never test
    // another worktree's code. CI is unchanged in effect (it already had
    // reuse disabled and starts a single server).
    command: `npm run dev -- --host 127.0.0.1 --port ${localServerPort} --strictPort`,
    url: localServerUrl,
    reuseExistingServer: false,
  } : undefined,
  globalSetup: require.resolve('./tests/e2e/setup/global-setup.ts'),
  globalTeardown: coverageEnabled ? './tests/e2e/setup/coverage-teardown.ts' : undefined,
})
