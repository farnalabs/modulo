import { type Page } from '@playwright/test'
import type { TestEnv } from './env'
import { completeLoginForm } from './fixtures'

/**
 * Sign in through the real login UI for the staging/app targets.
 *
 * Multi-org instances render an org-slug entry step at /login before the
 * email/password form.  Single-org instances render the credential form
 * directly.  The slug step is detected and completed automatically by
 * completeLoginForm(); callers never need to know which path the instance
 * takes.
 */
export async function loginThroughUi(page: Page, env: TestEnv): Promise<void> {
  await page.goto(env.credentials.loginPath)

  // Handles the multi-org slug step (if present) and guarantees the credential
  // form is on screen, failing fast with a diagnostic if it is not.
  await completeLoginForm(page, env)

  const emailInput = page.locator(env.credentials.loginFormEmailSelector)
  const passwordInput = page.locator(env.credentials.loginFormPasswordSelector)

  await emailInput.fill(env.credentials.admin.email)
  await passwordInput.fill(env.credentials.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60_000 })
}
