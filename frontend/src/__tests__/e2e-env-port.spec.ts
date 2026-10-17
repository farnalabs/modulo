/**
 * FAR-1900 / FAR-1922 — pin the local e2e port contract
 * (tests/e2e/setup/env.ts) and the playwright.config.ts defence-in-depth flags.
 *
 * The worktree-derived port stops a local e2e run silently reusing ANOTHER
 * worktree's dev server. `isCI()` short-circuits to 5173, so the entire non-CI
 * branch (the hash, the `E2E_PORT` override, the reserved-port skip, the local
 * `getBaseUrl` branch) is invisible to CI: a future revert to a hard-coded
 * port, a broken precedence, or a dropped reserved port would stay green while
 * reintroducing the cross-worktree reuse. This spec pins the contract.
 *
 * It imports the helpers directly from `tests/e2e/setup/env` (whose
 * `tests/e2e/**` files `vite.config.ts` excludes from the DEFAULT suite) rather
 * than re-implementing the derivation, so a change to the real code is what the
 * assertions observe. It also imports `RESERVED_LOCAL_E2E_PORTS` and the real
 * `playwright.config.ts` for the same reason: a hand-copied mirror would pass
 * while the shipped set/flags drifted.
 */
import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  RESERVED_LOCAL_E2E_PORTS,
  deriveLocalE2ePort,
  getBaseUrl,
  getLocalE2eBaseUrl,
  getLocalE2ePort,
} from '../../tests/e2e/setup/env'

const LOCAL_E2E_PORT_MIN = 4000
const LOCAL_E2E_PORT_MAX = 9999
const LOCAL_E2E_PORT_SPAN = LOCAL_E2E_PORT_MAX - LOCAL_E2E_PORT_MIN + 1

// env.ts reads these at call time. Snapshot + restore so this spec cannot leak
// target/port selection into a sibling spec in the same worker process.
const MANAGED_ENV = ['CI', 'E2E_PORT', 'E2E_BASE_URL', 'E2E_NO_WEBSERVER', 'E2E_TARGET'] as const
let savedEnv: Record<string, string | undefined>

beforeEach(() => {
  savedEnv = {}
  for (const key of MANAGED_ENV) {
    savedEnv[key] = process.env[key]
    delete process.env[key]
  }
})

afterEach(() => {
  for (const key of MANAGED_ENV) {
    if (savedEnv[key] === undefined) delete process.env[key]
    else process.env[key] = savedEnv[key]
  }
})

// Mirrors env.ts's derivation up to (but not including) the reserved-port skip,
// so the spec can choose a synthetic root that WOULD land on a reserved port
// and prove the skip actually happens.
function rawCandidatePort(worktreeRoot: string): number {
  const span = LOCAL_E2E_PORT_MAX - LOCAL_E2E_PORT_MIN + 1
  const digest = createHash('sha1').update(worktreeRoot).digest()
  return LOCAL_E2E_PORT_MIN + (digest.readUInt32BE(0) % span)
}

// The port env.ts's derivation SHOULD yield for a raw candidate: the candidate
// itself when free, else the first non-reserved port after it (wrapping). Kept
// independent of env.ts so a broken step-past is observed rather than mirrored.
function expectedDerivedPort(raw: number): number {
  let port = raw
  for (let i = 0; i < LOCAL_E2E_PORT_SPAN; i++) {
    if (!RESERVED_LOCAL_E2E_PORTS.includes(port)) return port
    port = port === LOCAL_E2E_PORT_MAX ? LOCAL_E2E_PORT_MIN : port + 1
  }
  throw new Error('[spec] no free port in range')
}

// Brute-force a synthetic worktree root whose raw hash lands on `port`. The
// range is 6000 wide, so ~6000 attempts is the expectation; the 500k bound
// makes a miss effectively impossible (per-port P(miss) ≈ e^-83).
function findRootForPort(port: number, maxAttempts = 500_000): string {
  for (let i = 0; i < maxAttempts; i++) {
    const root = `/synthetic/worktree-port-${port}-${i}`
    if (rawCandidatePort(root) === port) return root
  }
  throw new Error(`[spec] no synthetic worktree root hashed onto port ${port}`)
}

describe('local e2e port selection (FAR-1900)', () => {
  it('is 5173 in CI and ignores E2E_PORT', () => {
    process.env.CI = 'true'
    process.env.E2E_PORT = '4567'
    expect(getLocalE2ePort()).toBe(5173)
  })

  it('prefers a valid E2E_PORT when not in CI', () => {
    process.env.E2E_PORT = '4567'
    expect(getLocalE2ePort()).toBe(4567)
  })

  it('ignores an out-of-range E2E_PORT when not in CI', () => {
    process.env.E2E_PORT = '80'
    const port = getLocalE2ePort()
    expect(port).toBeGreaterThanOrEqual(LOCAL_E2E_PORT_MIN)
    expect(port).toBeLessThanOrEqual(LOCAL_E2E_PORT_MAX)
  })

  it('derives an integer port inside the documented range', () => {
    const port = getLocalE2ePort()
    expect(Number.isInteger(port)).toBe(true)
    expect(port).toBeGreaterThanOrEqual(LOCAL_E2E_PORT_MIN)
    expect(port).toBeLessThanOrEqual(LOCAL_E2E_PORT_MAX)
  })

  it('agrees with the URL Playwright polls (use.baseURL === webServer.url)', () => {
    const port = getLocalE2ePort()
    expect(getLocalE2eBaseUrl()).toBe(`http://127.0.0.1:${port}`)
    expect(getBaseUrl('local')).toBe(getLocalE2eBaseUrl())
  })

  it('is deterministic for a given worktree root and varies across roots', () => {
    const root = '/synthetic/worktree-determinism'
    expect(deriveLocalE2ePort(root)).toBe(deriveLocalE2ePort(root))
    // Distinct roots must not collapse onto one constant port — a hard-coded
    // port would satisfy the same-input case but fail this one.
    expect(deriveLocalE2ePort('/synthetic/worktree-4487')).not.toBe(
      deriveLocalE2ePort('/synthetic/worktree-3383'),
    )
  })

  it('steps past a reserved port deterministically and never derives one', () => {
    // These two synthetic roots hash onto 5173 and 8000 respectively; the
    // derivation must step to the next port.
    for (const root of ['/synthetic/worktree-4487', '/synthetic/worktree-3383']) {
      const port = deriveLocalE2ePort(root)
      expect(RESERVED_LOCAL_E2E_PORTS).not.toContain(port)
      expect(port).toBe(rawCandidatePort(root) + 1)
    }

    // Sweep: every derived port stays in range and off the reserved set.
    for (let i = 0; i < 2000; i++) {
      const port = deriveLocalE2ePort(`/synthetic/worktree-${i}`)
      expect(port).toBeGreaterThanOrEqual(LOCAL_E2E_PORT_MIN)
      expect(port).toBeLessThanOrEqual(LOCAL_E2E_PORT_MAX)
      expect(RESERVED_LOCAL_E2E_PORTS).not.toContain(port)
    }
  })

  it('reserves every fixed host listener the repo publishes inside the range', () => {
    // Each entry is a fixed host port this repo publishes or documents
    // (FAR-1922). A derived e2e port landing on one guarantees a listener
    // conflict. Pinned individually so dropping one fails loudly. Sources:
    //   4173 Vite `preview` (CONTRIBUTING.md + .github/workflows/bdd.yml)
    //   4317 otel-collector (docker-compose.local.yml)
    //   5173 frontend (docker-compose.yml)
    //   5432 Postgres (docker-compose.yml + docker-compose.local.yml)
    //   5433 Postgres (deploy/compose/docker-compose.test.yml)
    //   5434 Postgres (docker-compose.local.yml)
    //   6379 Redis (docker-compose.yml)
    //   6380 Redis (docker-compose.local.yml)
    //   8000 backend (docker-compose.yml)
    //   8081 saq-system (docker-compose.yml + docker-compose.local.yml)
    //   8082 watchdog (docker-compose.yml)
    //   8083 watchdog (deploy/compose/docker-compose.prod.yml)
    //   9090 Prometheus (docker-compose.local.yml)
    const fixedHostListeners = [4173, 4317, 5173, 5432, 5433, 5434, 6379, 6380, 8000, 8081, 8082, 8083, 9090]
    for (const port of fixedHostListeners) {
      expect(RESERVED_LOCAL_E2E_PORTS).toContain(port)
    }
  })

  it('steps past EVERY reserved port, not just the historical two', () => {
    // For each reserved port, construct a root whose raw hash lands exactly on
    // it and assert the derivation steps to the next non-reserved port. This
    // fails if a newly-added port is not actually skipped (e.g. a typo in the
    // set, or the step-past regressing to a fixed +1 that re-lands on a
    // neighbouring reserved port).
    for (const reserved of RESERVED_LOCAL_E2E_PORTS) {
      const root = findRootForPort(reserved)
      expect(rawCandidatePort(root)).toBe(reserved)
      expect(deriveLocalE2ePort(root)).toBe(expectedDerivedPort(reserved))
      expect(deriveLocalE2ePort(root)).not.toBe(reserved)
    }
  })
})

describe('playwright.config local webServer defence-in-depth (FAR-1922)', () => {
  it('disables server reuse and binds the derived port with --strictPort', async () => {
    // The derived port (pinned above) plus these two playwright.config.ts
    // values are the two layers that stop a local run attaching to a foreign
    // worktree's dev server. On a hash collision, `reuseExistingServer: false`
    // is what makes the run bind its OWN server or fail; `--strictPort` turns a
    // taken derived port into a loud startup error instead of a quiet port+1
    // move (which would leave Playwright polling — and silently reusing —
    // whatever held the requested port). A revert of either reopens silent
    // cross-worktree attachment while every other test stays green.
    //
    // Import the REAL config under a controlled local env rather than grepping
    // its source: a source-text grep would pass on any file and would not
    // observe the values Playwright actually consumes. `vi.resetModules()` +
    // dynamic import re-evaluates the config with CI/E2E_NO_WEBSERVER cleared,
    // so the assertion is independent of the environment the unit suite
    // happens to run under.
    delete process.env.CI
    delete process.env.E2E_PORT
    delete process.env.E2E_NO_WEBSERVER
    delete process.env.E2E_TARGET

    vi.resetModules()
    const [{ default: config }, env] = await Promise.all([
      import('../../playwright.config'),
      import('../../tests/e2e/setup/env'),
    ])

    const webServer = config.webServer
    if (!webServer || Array.isArray(webServer)) {
      throw new Error('[spec] expected a single local webServer config object')
    }
    expect(webServer.reuseExistingServer).toBe(false)
    expect(webServer.command).toContain('--strictPort')

    // The command and the polled URL must agree on the derived port: a run
    // that starts one port and polls another would silently test a foreign
    // server.
    const port = env.getLocalE2ePort()
    expect(webServer.url).toBe(`http://127.0.0.1:${port}`)
    expect(webServer.command).toContain(`--port ${port}`)
  })
})
