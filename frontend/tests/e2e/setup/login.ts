import { type Page } from '@playwright/test'
import type { TestEnv } from './env'

/**
 * Sign in through the real login UI for the staging/app targets.
 *
 * Multi-org instances render an org-slug entry step at /login instead of the
 * email/password form, so the target's per-org login path (`/login/<slug>`,
 * selected via E2E_ORG_SLUG) is used. Single-org instances keep /login, which
 * auto-skips to the direct form.
 */
export async function loginThroughUi(page: Page, env: TestEnv): Promise<void> {
  await page.goto(env.credentials.loginPath)
  await page.waitForSelector(env.credentials.loginFormEmailSelector, { timeout: 30000 })
  await page.fill(env.credentials.loginFormEmailSelector, env.credentials.admin.email)
  await page.fill(env.credentials.loginFormPasswordSelector, env.credentials.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60000 })
}
