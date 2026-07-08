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

const LOGIN_TIMEOUT: Record<string, number> = {
  local: 10000,
  staging: 20000,
  app: 20000,
}

export async function loginAsAdmin(page: Page, env: TestEnv) {
  // Catch-all: mock every /api/v1/* call to return 200 with empty data
  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    const method = route.request().method()
    if (url.includes('/api/v1/auth/login') || url.includes('/api/v1/auth/refresh')) {
      if (method === 'POST') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            access_token: 'mock-access-token-for-e2e-tests',
            refresh_token: 'mock-refresh-token-for-e2e-tests',
            token_type: 'bearer',
          }),
        })
      }
    }
    if (url.includes('/api/v1/me/settings')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ locale: 'en-US' }) })
    }
    if (url.includes('/api/v1/me')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: '1', email: 'admin@example.com', display_name: 'Admin' }) })
    }
    if (url.includes('/api/v1/pipelines') && method === 'GET') {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ items: [{ id: '1', name: 'Test Pipeline', status: 'idle', created_at: new Date().toISOString() }], total: 1 }),
      })
    }
    if (url.includes('/api/v1/admin/feature-flags')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ flags: {}, current_tier: 'enterprise' }) })
    }
    if (url.includes('/api/v1/auth/refresh')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ access_token: 'mock-access-token-for-e2e-tests', refresh_token: 'mock-refresh-token-for-e2e-tests', token_type: 'bearer' }),
      })
    }
    // Default: return empty 200 for any other API call
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/')
  await page.waitForLoadState('networkidle')
  await page.evaluate(() => {
    localStorage.setItem('modulo_access_token', 'mock-access-token-for-e2e-tests')
    localStorage.setItem('modulo_refresh_token', 'mock-refresh-token-for-e2e-tests')
  })
}
