import { test as base, expect, type Page } from '@playwright/test'
import { startCoverage, stopCoverage } from './coverage'
import { getTestEnv, getBaseUrl, type TestEnv } from './env'
import { resolveLoginPath } from './login-path'
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

const MOCK_ACCESS_TOKEN = 'mock-access-token-for-e2e-tests'
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

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
    if (url.includes('/api/v1/admin/feature-flags')) {
      // dev_mode: true so private_preview routes (evals, runs-diff, feedback
      // inbox, saved views, node categories, feature flags, monitoring,
      // error forwarders, runtime config, rate limits, ...) resolve on the
      // local mock-API target instead of redirecting to the dashboard. Local
      // e2e mirrors staging, which also runs with dev mode on.
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ flags: [], license: { tier: 'enterprise' }, dev_mode: true }) })
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
    if (method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) })
    }
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
}

/**
 * Advance past the multi-org slug-entry step (if present) so the
 * email/password credential form is on screen. Single-org instances render the
 * credential form directly on /login and this is a no-op. Must be called after
 * navigating to /login.
 */
export async function completeLoginForm(page: Page, env: TestEnv): Promise<void> {
  const slugInput = page.getByTestId('login-org-slug')
  const emailInput = page.locator(env.credentials.loginFormEmailSelector)

  const slugVisible = slugInput.waitFor({ state: 'visible', timeout: 30000 }).then(() => true).catch(() => false)
  const emailVisible = emailInput.waitFor({ state: 'visible', timeout: 30000 }).then(() => true).catch(() => false)

  const [gotSlug, gotEmail] = await Promise.all([slugVisible, emailVisible])

  if (!gotSlug && !gotEmail) {
    // Fail-fast: neither the slug selector nor the email form appeared.
    // Throw a rich diagnostic instead of silently returning and failing
    // opaquely at the next step.
    const currentURL = page.url()
    const pageTitle = await page.title()
    const bodyText = await page.locator('body').innerText().catch(() => '<unreadable>')
    const screenshot = await page.screenshot().catch(() => null)

    const diagnostic = [
      `[login] Neither slug selector nor email form appeared within 30s`,
      `[login] Current URL: ${currentURL}`,
      `[login] Page title: ${pageTitle}`,
      `[login] Page body (first 500 chars): ${bodyText.slice(0, 500)}`,
      `[login] E2E_ORG_SLUG=${process.env.E2E_ORG_SLUG || '(unset)'}`,
      screenshot ? '[login] Screenshot saved to test-results/login-diagnostic.png' : '',
    ]
      .filter(Boolean)
      .join('\n')

    if (screenshot) {
      const fs = await import('node:fs/promises')
      const path = await import('node:path')
      const dir = path.resolve('test-results')
      await fs.mkdir(dir, { recursive: true }).catch(() => {})
      await fs.writeFile(path.join(dir, 'login-diagnostic.png'), screenshot).catch(() => {})
    }

    throw new Error(diagnostic)
  }

  if (gotSlug) {
    await slugInput.fill(env.orgSlug)
    await page.getByTestId('login-org-entry-submit').click()
    await emailInput.waitFor({ state: 'visible', timeout: 30000 })
  }
}

/**
 * Navigate to the login page and ensure the credential form is ready to fill.
 * Use this instead of a bare `page.goto('/login')` whenever the test is about
 * to enter credentials, so the suite works on both single-org and multi-org
 * targets. Uses the dynamic resolver to pick the correct login path.
 */
export async function openLoginForm(page: Page, env: TestEnv): Promise<void> {
  const baseURL = getBaseUrl(env.name)
  const loginPath = await resolveLoginPath(baseURL)
  await page.goto(baseURL + loginPath)
  await completeLoginForm(page, env)
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

function b64url(input: unknown): string {
  return Buffer.from(JSON.stringify(input)).toString('base64url')
}

/**
 * Mint a real JWT whose payload marks the principal as a system admin
 * (is_system_admin + org_role admin). The local mock login token is not a JWT,
 * so the router guard cannot read the claim off it.
 */
export function systemAdminJwt(): string {
  const header = b64url({ alg: 'HS256', typ: 'JWT' })
  const now = Math.floor(Date.now() / 1000)
  const payload = b64url({
    sub: '1',
    email: 'admin@example.com',
    name: 'Admin',
    org_role: 'admin',
    is_system_admin: true,
    iat: now,
    exp: now + 3600,
  })
  return `${header}.${payload}.mocked-signature`
}

/**
 * Log in for a route in the SYSTEM sidebar group. FAR-938 makes the whole
 * group system-admin-only (UI + router).
 *
 * Returns true when the target can actually render such a route. The local
 * target uses the mock API and swaps in a system-admin JWT (same approach as
 * remy-only.spec.ts). Staging/prod authenticate with the real E2E_ADMIN_EMAIL
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
