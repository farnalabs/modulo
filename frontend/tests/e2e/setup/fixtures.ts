import { test as base, expect, type Page } from '@playwright/test'
import { startCoverage, stopCoverage } from './coverage'
import { getTestEnv, type TestEnv } from './env'
import { loginThroughUi } from './login'

export const test = base.extend<{ env: TestEnv }>({
  env: async ({}, use) => {
    await use(getTestEnv())
  },
  page: async ({ page }, use) => {
    const enabled = process.env.VITE_COVERAGE === 'true'
    if (enabled) await startCoverage(page)
    await use(page)
    if (enabled) await stopCoverage(page)
  },
})

export { expect }

export function isDevModeTarget(env: TestEnv): boolean {
  return env.name === 'local'
}

function b64url(input: unknown): string {
  return Buffer.from(JSON.stringify(input)).toString('base64url')
}

/**
 * The local target's mock login must issue a token with the SAME claim
 * contract as the real backend login (`POST /api/v1/auth/login` →
 * `create_access_token`): a JWT whose payload carries `sub` (account email),
 * `org_id`, `account_id`, `org_role` and `is_system_admin`.
 *
 * The SPA reads those claims straight off the token — `lib/jwt.ts` decodes it
 * for the router's role gate (`enforceRoleTierVisibility`), `useCurrentUser`
 * and `planStore.currentOrgId()` — and a non-JWT opaque string decodes to
 * `null`. With no readable `org_role`, EVERY route declaring
 * `required_roles: [admin]` (the ADMIN / CONFIGURE groups and the whole
 * `x-team` surface) was silently redirected to the dashboard, so its specs
 * failed asserting the wrong `<h1>`. The SPA never verifies the signature, so a
 * fixed placeholder third segment is sufficient — only the payload shape
 * matters.
 */
export function mintMockAccessToken(overrides: Record<string, unknown> = {}): string {
  const header = b64url({ alg: 'HS256', typ: 'JWT' })
  const now = Math.floor(Date.now() / 1000)
  const payload = b64url({
    sub: 'admin@example.com',
    org_id: '1',
    account_id: '1',
    org_role: 'admin',
    is_system_admin: false,
    ...overrides,
    iat: now,
    exp: now + 3600,
  })
  return `${header}.${payload}.mocked-signature`
}

// A regular org-admin session — the local analogue of the staging
// E2E_ADMIN_* identity, which is also a non-system org admin.
const MOCK_ACCESS_TOKEN = mintMockAccessToken()
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

// Shared date spine for the dashboard summary/trends fixtures. Kept as data
// (rather than inlined per series) so the seven trends arrays stay the same
// length and every series lines up on the same dates for the charts.
const DASHBOARD_TREND_DATES = [
  '2026-07-01', '2026-07-02', '2026-07-03', '2026-07-04',
  '2026-07-05', '2026-07-06', '2026-07-07',
]

export async function setupLocalMockApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    const method = route.request().method()
    if ((url.includes('/api/v1/auth/login') || url.includes('/api/v1/auth/refresh')) && method === 'POST') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ access_token: MOCK_ACCESS_TOKEN, refresh_token: MOCK_REFRESH_TOKEN, token_type: 'bearer' }),
      })
    }
    if (url.includes('/api/v1/me/settings')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ locale: 'en-US' }) })
    }
    if (url.includes('/api/v1/me')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: '1', email: 'admin@example.com', display_name: 'Admin' }) })
    }
    if (url.includes('/api/v1/pipelines') && method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: '1', name: 'Test Pipeline', organisation_id: '1', description: 'A test pipeline', visibility: 'org', status: 'idle', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), archived_at: null }], total: 1 }) })
    }
    if (url.includes('/api/v1/plan') || url.includes('/api/v1/admin/feature-flags')) {
      // planStore bootstraps from the caller-scoped GET /api/v1/plan (FAR-1809
      // collapsed the old instance-admin /api/v1/admin/feature-flags reads into
      // it); the instance-admin endpoint still backs the admin feature-flags
      // page, so answer both with the plan payload shape.
      // dev_mode: true so private_preview routes (evals, runs-diff, feedback
      // inbox, saved views, node categories, feature flags, monitoring,
      // error forwarders, runtime config, rate limits, ...) resolve on the
      // local mock-API target instead of redirecting to the dashboard. Local
      // e2e mirrors staging, which also runs with dev mode on.
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ flags: [], license: { tier: 'enterprise' }, dev_mode: true }) })
    }
    if (url.includes('/api/v1/plan')) {
      // FAR-1809: the caller-scoped plan bootstrap (`GET /api/v1/plan`) that
      // replaced the instance-admin `/admin/feature-flags` + `/admin/license`
      // pair the app's planStore used to read. Same vocabulary (tier, licence
      // view, dev_mode, effective flag map) — dev_mode: true so private_preview
      // routes resolve on the local mock-API target instead of redirecting to
      // the dashboard (local e2e mirrors staging, which also runs dev mode on).
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ flags: [], license: { tier: 'enterprise', org_id: '1', expires_at: null }, dev_mode: true }) })
    }
    if (url.includes('/api/v1/admin/license')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ expires_at: null, org_id: '1', tier: 'enterprise' }) })
    }
    if (url.includes('/api/v1/admin/tiers')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tiers: [{ tier_id: 'community', label: 'Community', rank: 0 }, { tier_id: 'team', label: 'Team', rank: 1 }, { tier_id: 'enterprise', label: 'Enterprise', rank: 2 }] }) })
    }
    if (url.includes('/api/v1/views')) {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { id: '1', name: 'Active Runs', view_type: 'table', columns: ['name', 'status'], filters: {}, sort_by: 'name', sort_order: 'asc', created_by: 'alice@test.com', created_at: new Date().toISOString() },
            { id: '2', name: 'Kanban Board', view_type: 'grid', columns: ['name', 'status'], filters: {}, sort_by: 'name', sort_order: 'asc', created_by: 'bob@test.com', created_at: new Date().toISOString() },
          ],
          total: 2,
        }),
      })
    }
    if (url.includes('/api/v1/dashboard/summary') && method === 'GET') {
      // DashboardView calls this on mount (with `days=<n>` for a rolling
      // window). serve the same populated body for both shapes — the summary
      // validators require total_runs/active_pipelines/run_counts_by_status/
      // teams/recent_runs, and the arrays must not be empty or the view falls
      // through to its error/empty branches. Registered BEFORE the generic GET
      // catch-all below so it is actually reachable.
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total_runs: 128,
          active_pipelines: 4,
          run_counts_by_status: { running: 1, awaiting_human: 0, failed: 3, idle: 124 },
          eval_pass_rate: {
            overall_pass_rate: 92,
            total_evals: 50,
            passed_evals: 46,
            per_pipeline: {},
            per_team_pipeline: {},
          },
          trend: DASHBOARD_TREND_DATES.map((date, i) => ({
            date,
            run_count: 12 + i * 3,
            eval_pass_rate: 88 + i,
            token_spend_usd: 3.5 + i * 0.75,
            avg_time_to_approve_ms: 120000 + i * 5000,
            rejection_rate: 5 + i,
          })),
          teams: [
            {
              id: '1',
              name: 'Platform',
              total_runs: 128,
              active_pipelines: 4,
              run_counts_by_status: { running: 1, awaiting_human: 0, failed: 3, idle: 124 },
              eval_pass_rate: { total_evals: 50, passed_evals: 46, pass_rate: 92 },
            },
          ],
          recent_runs: [
            { id: '101', pipeline_name: 'Release Pipeline', status: 'success', created_at: '2026-07-07T09:15:00Z', trigger_type: 'webhook', execution_origin: null },
            { id: '102', pipeline_name: 'Nightly Regression', status: 'failed', created_at: '2026-07-07T06:00:00Z', trigger_type: 'cron', execution_origin: 'dispatched' },
            { id: '103', pipeline_name: 'Docs Build', status: 'running', created_at: '2026-07-07T11:30:00Z', trigger_type: 'manual', execution_origin: null },
          ],
          config_warnings: [],
        }),
      })
    }
    if (url.includes('/api/v1/dashboard/trends') && method === 'GET') {
      // Seven required series, each an ARRAY (validateTrendsResponse rejects
      // any missing/non-array key). Populated wherever the chart reads a value;
      // every series shares DASHBOARD_TREND_DATES so the combined trend in
      // DashboardView renders a full curve. Registered BEFORE the generic GET
      // catch-all so it is actually reachable.
      // Every rejection-rate field is a PERCENTAGE (0-100), matching the
      // backend's `round(rejected / total * 100, 1)` — never a 0-1 fraction.
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          days: DASHBOARD_TREND_DATES.length,
          run_counts: DASHBOARD_TREND_DATES.map((date, i) => ({ date, run_count: 12 + i * 3 })),
          eval_pass_rates: DASHBOARD_TREND_DATES.map((date, i) => ({ date, total_evals: 8, passed_evals: 7, pass_rate: 88 + i })),
          token_spend: DASHBOARD_TREND_DATES.map((date, i) => ({ date, total_spend_usd: 3.5 + i * 0.75 })),
          hitl_volume: DASHBOARD_TREND_DATES.map((date, i) => ({ date, total_decisions: 4, approved_count: 3, rejected_count: 1, rejection_rate: 25, avg_time_to_approve_ms: 120000 + i * 5000 })),
          rejection_trend: DASHBOARD_TREND_DATES.map((date, i) => ({ date, rolling_rejection_rate: 5 + i, raw_rejection_rate: 6 + i })),
          correlation: DASHBOARD_TREND_DATES.map((date, i) => ({ date, rejection_rate: 5 + i, eval_pass_rate: 88 + i })),
          feedback_volume: DASHBOARD_TREND_DATES.map((date) => ({ date, feedback_count: 2, resolved_count: 1, correcting_count: 1 })),
        }),
      })
    }
    if (
      (url.includes('/api/v1/pipeline-folders') || url.includes('/api/v1/schema-folders')) &&
      method === 'GET'
    ) {
      // Folder-list endpoints return a BARE ARRAY, not the generic
      // {items,total} envelope. The consumers type the response as
      // FolderItem[] and iterate it in a computed — FolderTree.vue
      // (`allFolders.value.filter`) and PipelineListView.vue /
      // SchemaListView.vue (`for (const f of foldersList.value)`) — so the
      // envelope makes the computed throw `TypeError: ... is not a function`
      // / `... is not iterable` and silently blanks the folder sidebar
      // (FAR-1876). Mirrors `list_folders` in
      // backend/src/modulo/api/routes/{pipeline_folders,schema_folders}.py,
      // which returns `list[FolderResponse]`. Registered BEFORE the generic
      // GET catch-all below so it is actually reachable.
      return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    }
    if (url.includes('/api/v1/runners/status') && method === 'GET') {
      // `RunnersStatusResponse` is a STRUCTURED contract, not the generic
      // {items,total} envelope: `RunnersConcurrencyTab` reads
      // `status.concurrency.preflight` (FAR-591 D5), so the envelope makes
      // `.preflight` throw `TypeError: Cannot read properties of undefined` and
      // blanks the whole Concurrency tab. Mirrors
      // `components["schemas"]["RunnersStatusResponse"]` in the generated
      // OpenAPI types (frontend/src/lib/runnersStatus.ts derives from it).
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          aggregate_state: 'healthy',
          probe_interval_seconds: 60,
          staleness_threshold_seconds: 300,
          machines: [],
          profiles: [],
          concurrency: {
            sandbox_concurrency_limit: 4,
            is_default: true,
            preflight: {
              state: 'ok',
              detail: 'Within engine capacity',
              engine_cpu_count: 8,
              engine_mem_total_mb: 16384,
              needed_cpu: 4,
              needed_mem_mb: 4096,
            },
          },
        }),
      })
    }
    if (url.includes('/api/v1/admin/org/sandbox-concurrency') && method === 'GET') {
      // `SandboxConcurrencyResponse` (FAR-589 D3b) — the Concurrency tab's
      // limit read. `is_default` marks the ABSENT-key case so the UI can
      // render "4 (default)" rather than "no gate".
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ sandbox_concurrency_limit: 4, is_default: true }),
      })
    }
    if (method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) })
    }
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
}

// Budgets for the login handshake.  The step race is generous enough for a
// cold SPA boot; the credential-form guard is tighter so a broken login page
// aborts in seconds instead of letting every test retry for 30 s.
const LOGIN_STEP_TIMEOUT = 15_000
const CREDENTIAL_FORM_TIMEOUT = 10_000

/**
 * The credential-form selectors actually rendered by the target. Two layouts
 * provide a credential form:
 *  - LoginView's direct branch (`login-email`/`login-password`) on single-org
 *    instances; and
 *  - OrgLoginView (`org-login-email`/`org-login-password`) on multi-org
 *    instances, reached after the slug step navigates to /login/<slug>.
 * Callers must fill/submit whichever one rendered, not assume LoginView's.
 */
export interface CredentialFormSelectors {
  email: string
  password: string
  submit: string
}

function singleOrgCredentialForm(env: TestEnv): CredentialFormSelectors {
  return {
    email: env.credentials.loginFormEmailSelector,
    password: env.credentials.loginFormPasswordSelector,
    submit: env.credentials.loginFormSubmitSelector,
  }
}

function orgCredentialForm(env: TestEnv): CredentialFormSelectors {
  return {
    email: env.credentials.orgLoginFormEmailSelector,
    password: env.credentials.orgLoginFormPasswordSelector,
    submit: env.credentials.orgLoginFormSubmitSelector,
  }
}

/**
 * Fail fast unless a credential (email/password) form renders within the
 * budget, and return the selectors for whichever layout appeared. Shared by
 * every login entry point so a broken login page produces one actionable
 * diagnostic instead of a 30 s timeout per test.
 */
async function requireCredentialForm(
  page: Page,
  env: TestEnv,
  slugStepTaken: boolean,
): Promise<CredentialFormSelectors> {
  const candidates = [singleOrgCredentialForm(env), orgCredentialForm(env)]
  // Wait once for whichever layout's email field appears first. A single
  // union selector avoids racing two waitFor()s and leaving the loser's
  // timer dangling for the rest of the budget after one resolves.
  const emailSelectors = candidates.map((form) => form.email).join(', ')
  const appeared = await page
    .locator(emailSelectors)
    .first()
    .waitFor({ state: 'visible', timeout: CREDENTIAL_FORM_TIMEOUT })
    .then(() => true)
    .catch(() => false)

  if (appeared) {
    // Disambiguate which layout rendered so callers fill its own fields.
    for (const form of candidates) {
      if (await page.locator(form.email).isVisible()) return form
    }
  }

  const hint = slugStepTaken
    ? `the org slug "${env.orgSlug}" may be wrong, or /login/${env.orgSlug} did not render a credential form`
    : 'the login page layout or login-context API may have changed'
  throw new Error(
    `[login] Credential form did not appear within ${CREDENTIAL_FORM_TIMEOUT}ms ` +
    `(slugStepTaken=${slugStepTaken}). Neither ${env.credentials.loginFormEmailSelector} ` +
    `nor ${env.credentials.orgLoginFormEmailSelector} became visible. ` +
    `Current URL: ${page.url()}. ${hint}. ` +
    'Every test that signs in will fail the same way.',
  )
}

/**
 * Advance past the multi-org slug-entry step (if present) and guarantee the
 * email/password credential form is on screen. Single-org instances render the
 * credential form directly on /login; multi-org instances render OrgLoginView's
 * form after the slug step. Returns the selectors for whichever form appeared,
 * and throws a diagnostic if neither does, so every caller fails fast instead
 * of timing out per test. Must be called after navigating to /login.
 */
export async function completeLoginForm(page: Page, env: TestEnv): Promise<CredentialFormSelectors> {
  const slugInput = page.locator(env.credentials.orgSlugInputSelector)
  const emailInput = page.locator(env.credentials.loginFormEmailSelector)

  // Race: whichever appears first wins — the slug step on multi-org targets,
  // the credential form on single-org targets.
  await Promise.race([
    slugInput.waitFor({ state: 'visible', timeout: LOGIN_STEP_TIMEOUT }).catch(() => {}),
    emailInput.waitFor({ state: 'visible', timeout: LOGIN_STEP_TIMEOUT }).catch(() => {}),
  ])

  const slugStepTaken = await slugInput.isVisible()
  if (slugStepTaken) {
    await slugInput.fill(env.orgSlug)
    await page.locator(env.credentials.orgSlugSubmitSelector).click()
  }

  return requireCredentialForm(page, env, slugStepTaken)
}

/**
 * Navigate to the login page and ensure the credential form is ready to fill.
 * Use this instead of a bare `page.goto('/login')` whenever the test is about
 * to enter credentials, so the suite works on both single-org and multi-org
 * targets. Returns the selectors for whichever form rendered.
 */
export async function openLoginForm(page: Page, env: TestEnv): Promise<CredentialFormSelectors> {
  await page.goto('/login')
  return completeLoginForm(page, env)
}

export async function loginAsAdmin(page: Page, env: TestEnv) {
  if (env.name !== 'local') {
    await loginThroughUi(page, env)
    return
  }

  await setupLocalMockApi(page)
  await page.goto('/login')
  await page.evaluate(([token, refresh]) => {
    localStorage.setItem('modulo_access_token', token)
    localStorage.setItem('modulo_refresh_token', refresh)
  }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN])
}

/**
 * Mint a real JWT whose payload marks the principal as a system admin
 * (is_system_admin + org_role admin). The local mock login token is a plain org
 * admin, so the router's SYSTEM-group gate (`requiresSystemAdmin`) redirects
 * until this is swapped in.
 */
export function systemAdminJwt(): string {
  return mintMockAccessToken({ is_system_admin: true })
}

/**
 * Log in for a route in the SYSTEM sidebar group. FAR-938 makes the whole
 * group system-admin-only (UI + router).
 *
 * Returns true when the target can actually render such a route. The local
 * target uses the mock API and swaps in a system-admin JWT (same approach as
 * assistant-only.spec.ts). Staging/prod authenticate with the real E2E_ADMIN_EMAIL
 * identity, which is a regular org admin — there is no in-product way to grant
 * is_system_admin — so the router guard redirects SYSTEM routes to the
 * dashboard and this returns false.
 */
export async function loginForSystemRoute(page: Page, env: TestEnv): Promise<boolean> {
  await loginAsAdmin(page, env)
  if (env.name !== 'local') return false
  await page.evaluate((token) => {
    localStorage.setItem('modulo_access_token', token)
  }, systemAdminJwt())
  return true
}
