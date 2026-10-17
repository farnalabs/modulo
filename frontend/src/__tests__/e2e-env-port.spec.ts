/**
 * FAR-1900 — pin the local e2e port contract (tests/e2e/setup/env.ts).
 *
 * The worktree-derived port is the guard that stops a local e2e run silently
 * reusing ANOTHER worktree's dev server. `isCI()` short-circuits to 5173, so
 * the entire non-CI branch (the hash, the `E2E_PORT` override, the local
 * `getBaseUrl` branch) is invisible to every CI job: a future revert to a
 * hard-coded port, or a broken precedence, would stay green in CI while
 * reintroducing the cross-worktree reuse. This spec pins the contract.
 *
 * It imports the helper directly from `tests/e2e/setup/env` (whose
 * `tests/e2e/**` files `vite.config.ts` excludes from the DEFAULT suite) rather
 * than re-implementing the derivation, so a change to the real code is what the
 * assertions observe.
 */
import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  deriveLocalE2ePort,
  getBaseUrl,
  getLocalE2eBaseUrl,
  getLocalE2ePort,
} from '../../tests/e2e/setup/env'

const LOCAL_E2E_PORT_MIN = 4000
const LOCAL_E2E_PORT_MAX = 9999

// Kept in lockstep with env.ts's RESERVED_LOCAL_E2E_PORTS; the spec asserts the
// derivation never yields one of these.
const RESERVED_LOCAL_E2E_PORTS = [5173, 8000]

// env.ts reads these at call time. Snapshot + restore so this spec cannot leak
// target/port selection into a sibling spec in the same worker process.
const MANAGED_ENV = ['CI', 'E2E_PORT', 'E2E_BASE_URL', 'E2E_NO_WEBSERVER'] as const
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
})
