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
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill(env.credentials.loginFormEmailSelector, env.credentials.admin.email)
  await page.fill(env.credentials.loginFormPasswordSelector, env.credentials.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(url => !url.includes('/login'), { timeout: LOGIN_TIMEOUT[env.name] || 15000 })
}
