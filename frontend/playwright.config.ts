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

// Resolved Playwright worker count — the rationale lives with the `workers`
// property below. Staging is serial; a local run is capped; app and CI keep
// Playwright's default (undefined). `--workers=N` still overrides on the CLI.
let workers: number | undefined
if (target === 'staging') {
  workers = 1
} else if (target === 'local') {
  workers = 2
}

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
  // Local runs share this machine with several concurrent agents, so
  // Playwright's default worker count (half the logical CPUs — 8 on a
  // 16-vCPU box) oversubscribes it: the run's own workers, the dev server they
  // all hit, and the other agents' processes contend for the same cores. Cap
  // local parallelism at 2 so a local run bounds its own footprint instead of
  // competing for every core.
  //
  // Re-verified 2026-10-17 on main@06b1e20ee (324 tests), 3 full runs at the
  // effective 2 workers: 58 / 58 / 57 failures in 18.5 / 16.1 / 13.0 min.
  // The failing set has a 56-test core; the only variation is 3 tests, all
  // genuine intermittent or environment-dependent failures — app-bootstrap
  // (502 with no backend), dialog-select-overlay and assistant-select
  // (Select-overlay races) — not contention timeouts. The 56-test core is the
  // FAR-1898 residual: 20 fail on the admin-route redirect (the local mock
  // access token is not a JWT, so `required_roles: [admin]` routes render the
  // dashboard) and the rest are the other local fixture/harness gaps that
  // ticket classified. PR #1922 fixes those gaps; a worker cap cannot make a
  // local run green.
  //
  // An earlier attempt in this worktree (pre-rebase, 5 runs at 8 workers and 5
  // at 2 across two bases) reported the 8- and 2-worker failure cores
  // identical and no demonstrable jitter reduction at 2, at ~1.4x the
  // wall-clock. Treat the cap as footprint-bounding hygiene, not as a
  // determinism fix.
  //
  // Staging stays serial (1); app keeps Playwright's default; `--workers=N`
  // still overrides on the CLI (e.g. the `test:e2e:mobile` script).
  workers,
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
