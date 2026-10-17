import { createHash } from 'node:crypto'
import { resolve } from 'node:path'

export interface TestEnv {
  name: 'local' | 'staging' | 'app'
  // Multi-org instances (FAR-856/857) render a slug-entry step on /login
  // before the email/password form. The E2E admin always belongs to the
  // instance's first (default) org; override with E2E_ORG_SLUG when a target
  // uses a different slug. Single-org instances ignore this entirely.
  orgSlug: string
  credentials: {
    admin: { email: string; password: string }
    // Single-org credential form — LoginView's direct branch on /login.
    loginFormEmailSelector: string
    loginFormPasswordSelector: string
    loginFormSubmitSelector: string
    // Per-org credential form — OrgLoginView, reached after the slug step
    // navigates to /login/<slug>. Multi-org targets render this form, not
    // LoginView's, so completeLoginForm() resolves whichever appeared.
    orgLoginFormEmailSelector: string
    orgLoginFormPasswordSelector: string
    orgLoginFormSubmitSelector: string
    // Multi-org slug-entry step — LoginView's branch when more than one org
    // is login-active.
    orgSlugInputSelector: string
    orgSlugSubmitSelector: string
    // Always /login.  On multi-org instances the slug step is completed by
    // completeLoginForm(); on single-org instances the credential form renders
    // directly.  Never navigate to /login/<slug> from E2E — the slug step
    // must be handled programmatically so the same code path covers both.
    loginPath: string
  }
}

const TESTID_SELECTORS = {
  email: '[data-testid="login-email"]',
  password: '[data-testid="login-password"]',
  submit: '[data-testid="login-submit"]',
  orgEmail: '[data-testid="org-login-email"]',
  orgPassword: '[data-testid="org-login-password"]',
  orgSubmit: '[data-testid="org-login-submit"]',
  orgSlugInput: '[data-testid="login-org-slug"]',
  orgSlugSubmit: '[data-testid="login-org-entry-submit"]',
}

// The app's data-testids and the login path are identical across local,
// staging and app, so the nine credential-form fields are defined once and
// spread into each target's `credentials` instead of repeated verbatim.
type CredentialSelectors = Pick<
  TestEnv['credentials'],
  | 'loginFormEmailSelector'
  | 'loginFormPasswordSelector'
  | 'loginFormSubmitSelector'
  | 'orgLoginFormEmailSelector'
  | 'orgLoginFormPasswordSelector'
  | 'orgLoginFormSubmitSelector'
  | 'orgSlugInputSelector'
  | 'orgSlugSubmitSelector'
  | 'loginPath'
>

const SHARED_CREDENTIALS: CredentialSelectors = {
  loginFormEmailSelector: TESTID_SELECTORS.email,
  loginFormPasswordSelector: TESTID_SELECTORS.password,
  loginFormSubmitSelector: TESTID_SELECTORS.submit,
  orgLoginFormEmailSelector: TESTID_SELECTORS.orgEmail,
  orgLoginFormPasswordSelector: TESTID_SELECTORS.orgPassword,
  orgLoginFormSubmitSelector: TESTID_SELECTORS.orgSubmit,
  orgSlugInputSelector: TESTID_SELECTORS.orgSlugInput,
  orgSlugSubmitSelector: TESTID_SELECTORS.orgSlugSubmit,
  loginPath: '/login',
}

function getOrgSlug(): string {
  return process.env.E2E_ORG_SLUG || 'default'
}

const ENVS: Record<string, TestEnv> = {
  local: {
    name: 'local',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: 'admin@example.com', password: 'password123' },
      ...SHARED_CREDENTIALS,
    },
  },
  staging: {
    name: 'staging',
    orgSlug: getOrgSlug(),
    credentials: {
      // Staging uses the real admin account, provided via E2E_ADMIN_EMAIL /
      // E2E_ADMIN_PASSWORD (must match the deployment's MODULO_USERS).
      admin: { email: process.env.E2E_ADMIN_EMAIL || '', password: process.env.E2E_ADMIN_PASSWORD || '' },
      ...SHARED_CREDENTIALS,
    },
  },
  app: {
    name: 'app',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: process.env.E2E_ADMIN_EMAIL || '', password: process.env.E2E_ADMIN_PASSWORD || 'admin123' },
      ...SHARED_CREDENTIALS,
    },
  },
}

export const BASE_URLS: Record<string, string> = {
  local: 'http://127.0.0.1:5173',
  staging: 'https://staging.modulo.run',
  app: 'https://app.modulo.run',
}

// ---------------------------------------------------------------------------
// Local e2e port selection (FAR-1900)
//
// Many worktrees / agents run concurrently on one machine. A fixed local port
// (5173) plus `reuseExistingServer: !CI` let Playwright silently attach to a
// DIFFERENT worktree's dev server and test that worktree's code — a
// screenshot-backed failure that does not exist on the branch under test.
//
// A local run now derives a stable, worktree-unique port. Combined with
// `strictPort: true` and `reuseExistingServer: false` in playwright.config.ts,
// a local run either binds ITS OWN server or fails loudly at startup — it can
// never silently reuse a foreign server.
//
// Port precedence, highest first:
//   1. CI — always 5173, IGNORING `E2E_PORT`. CI runs a single checkout on a
//      single runner, so the cross-worktree collision this guards against
//      cannot occur there, and CI's behaviour must not change. An `E2E_PORT`
//      set in CI is deliberately discarded rather than honoured.
//   2. `E2E_PORT` (non-CI only) — an explicit valid port (1024-65535) wins.
//   3. The worktree-derived port.
// `E2E_BASE_URL` and `E2E_NO_WEBSERVER` continue to work exactly as before.
// ---------------------------------------------------------------------------
const LOCAL_E2E_PORT_MIN = 4000
const LOCAL_E2E_PORT_MAX = 9999

// Ports the derivation must never yield. 5173 is the historical local
// dev-server port (what a hand-started `pnpm dev` uses, and the URL a
// `E2E_NO_WEBSERVER=true` run still targets); 8000 is the backend dev API the
// Vite dev server proxies `/api` to. A derived port colliding with either
// would guarantee a real listener conflict, so the derivation steps past them.
const RESERVED_LOCAL_E2E_PORTS: readonly number[] = [5173, 8000]

export function isCI(): boolean {
  return Boolean(process.env.CI)
}

function isReservedLocalE2ePort(port: number): boolean {
  return RESERVED_LOCAL_E2E_PORTS.includes(port)
}

// Deterministically maps a worktree root to a port in
// [LOCAL_E2E_PORT_MIN, LOCAL_E2E_PORT_MAX], skipping reserved ports. Pure and
// stable: the same root always yields the same port, so the dev server a run
// starts and the URL it polls cannot drift apart. Exported so the contract can
// be pinned by a unit spec (frontend/src/__tests__/e2e-env-port.spec.ts).
export function deriveLocalE2ePort(worktreeRoot: string): number {
  const span = LOCAL_E2E_PORT_MAX - LOCAL_E2E_PORT_MIN + 1
  const digest = createHash('sha1').update(worktreeRoot).digest()
  let port = LOCAL_E2E_PORT_MIN + (digest.readUInt32BE(0) % span)
  for (let i = 0; i < span; i++) {
    if (!isReservedLocalE2ePort(port)) return port
    port = port === LOCAL_E2E_PORT_MAX ? LOCAL_E2E_PORT_MIN : port + 1
  }
  // Unreachable while the reserved set is smaller than the range; fail loud
  // rather than return a reserved port if that ever stops being true.
  throw new Error('[env] local e2e port derivation exhausted the range')
}

export function getLocalE2ePort(): number {
  if (isCI()) return 5173

  const explicit = Number.parseInt(process.env.E2E_PORT ?? '', 10)
  if (Number.isInteger(explicit) && explicit >= 1024 && explicit <= 65535) return explicit

  // Hash the worktree root (this file lives at <worktree>/frontend/tests/e2e/
  // setup/env.ts), which is unique per worktree, into a stable port. The range
  // is wide enough that simultaneous worktrees almost never collide; if they
  // ever do, `strictPort` turns the collision into a loud startup error rather
  // than a silent cross-worktree reuse.
  const worktreeRoot = resolve(__dirname, '..', '..', '..', '..')
  return deriveLocalE2ePort(worktreeRoot)
}

export function getLocalE2eBaseUrl(): string {
  return `http://127.0.0.1:${getLocalE2ePort()}`
}

export function getTarget(): string {
  return (process.env.E2E_TARGET || 'local').toLowerCase()
}

export function getBaseUrl(target?: string): string {
  const t = target ?? getTarget()
  if (process.env.E2E_BASE_URL) return process.env.E2E_BASE_URL
  if (t === 'local') {
    // A caller that opted out of Playwright's webServer is managing its own
    // server; honour the historical fixed local URL so `E2E_NO_WEBSERVER=true`
    // on its own still targets a hand-started server on 5173. This is an
    // explicit opt-out, not the silent-reuse path the guard closes.
    const noServer = (process.env.E2E_NO_WEBSERVER || '').toLowerCase() === 'true'
    return noServer ? BASE_URLS.local : getLocalE2eBaseUrl()
  }
  return BASE_URLS[t] || BASE_URLS.local
}

// Read-only opt-out for non-local runs. `global-setup.ts` otherwise seeds the
// target with a test pipeline plus retention / email / cost-control settings
// before logging in — a capture of a real deployment must be able to run
// without writing anything to it. True iff `E2E_SKIP_SEED` trims/case-folds to
// one of `1` / `true` / `yes` / `on`; false when unset, empty, or any other
// value, so leaving the flag out preserves today's seeding behaviour exactly.
// Dependency-free by design — it is read on the global-setup path before any
// target work happens.
export function shouldSkipSeed(): boolean {
  const raw = (process.env.E2E_SKIP_SEED ?? '').trim().toLowerCase()
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on'
}

export function getTestEnv(): TestEnv {
  const target = getTarget()
  if (!ENVS[target]) {
    console.warn(`[env] Unknown E2E_TARGET "${process.env.E2E_TARGET}", falling back to "local". Valid values: ${Object.keys(ENVS).join(', ')}`)
  }
  return ENVS[target] || ENVS.local
}
