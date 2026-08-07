import { test as base, expect, type Page } from '@playwright/test'
import { startCoverage, stopCoverage } from './coverage'
import { getTestEnv, type TestEnv } from './env'

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

export async function loginAsAdmin(page: Page, env: TestEnv) {
  if (env.name !== 'local') {

    await page.goto('/login')
    await page.waitForSelector(env.credentials.loginFormEmailSelector, { timeout: 15000 })
    await page.fill(env.credentials.loginFormEmailSelector, env.credentials.admin.email)
    await page.fill(env.credentials.loginFormPasswordSelector, env.credentials.admin.password)
    await page.click('button[type="submit"]')
    await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60000 })
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
 * Mock the plan context (feature-flags, license, tiers) so the app boots with
 * team tier and dev-mode enabled. Backend has no "enterprise" tier — team is
 * the highest. This lets private_preview / team-tier routes pass the router
 * guard regardless of the real staging org tier.
 */
export async function mockEnterprisePlan(page: Page) {
  await page.route('**/api/v1/admin/feature-flags', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ flags: [], license: { tier: 'team' }, dev_mode: true }),
    })
  })
  await page.route('**/api/v1/admin/license', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ expires_at: null, org_id: '1', tier: 'team' }),
    })
  })
  await page.route('**/api/v1/admin/tiers', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tiers: [{ tier_id: 'community', label: 'Community', rank: 0 }, { tier_id: 'team', label: 'Team', rank: 1 }] }),
    })
  })
}

async function spaNavigate(page: Page, path: string) {
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.evaluate((target) => {
      const app = (document.querySelector('#app') as unknown as {
        __vue_app__?: { config: { globalProperties: { $router?: { push: (to: string) => void } } } }
      }).__vue_app__
      if (app?.config.globalProperties.$router) {
        app.config.globalProperties.$router.push(target)
      } else {
        history.pushState({}, '', target)
        window.dispatchEvent(new PopStateEvent('popstate'))
      }
    }, path)
    try {
      await page.waitForURL(
        (url) => url.pathname === path || url.pathname.startsWith(path + '/'),
        { timeout: 10000 },
      )
      return
    } catch {
      // Plan store may not have been applied yet; flush the event loop and retry
      await page.evaluate(() => new Promise<void>((resolve) => setTimeout(resolve, 0)))
    }
  }
  throw new Error('SPA navigation to ' + path + ' failed after retries')
}

/**
 * Login and navigate to a private_preview / team-tier route. On a full page
 * load the router guard runs before planStore.fetchPlan resolves, so a direct
 * page.goto() to a gated route redirects to the dashboard. This helper loads a
 * stable route first so the plan store settles with the mocked dev-mode / team
 * tier, then navigates client-side (SPA) so the guard passes.
 */
export async function loginAndGotoEnterprise(page: Page, env: TestEnv, path: string) {
  if (env.name === 'local') {
    await setupLocalMockApi(page)
  } else {
    await mockEnterprisePlan(page)
  }
  await loginAsAdmin(page, env)
  // Full-load a stable route so planStore.fetchPlan runs against the mocked
  // plan and settles deterministically (waitForResponse registered up-front).
  const planLoaded = page.waitForResponse((r) => r.url().includes('/api/v1/admin/feature-flags'))
  await page.goto('/dashboard')
  await planLoaded
  await page.evaluate(() => new Promise<void>((resolve) => setTimeout(resolve, 0)))
  await spaNavigate(page, path)
}

export { spaNavigate }
