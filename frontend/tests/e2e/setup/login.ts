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

  // Handle the multi-org slug step (if present) and wait for the credential
  // form.  completeLoginForm is a no-op when the slug step is absent.
  await completeLoginForm(page, env)

  // The credential form is now on screen — fill and submit.
  const emailInput = page.locator(env.credentials.loginFormEmailSelector)
  const passwordInput = page.locator(env.credentials.loginFormPasswordSelector)

  // Guard: if the credential form is still not visible, fail fast with a
  // diagnostic rather than waiting 30 s per test × retries.
  const emailVisible = await emailInput.waitFor({ state: 'visible', timeout: 10_000 }).then(() => true).catch(() => false)
  if (!emailVisible) {
    throw new Error(
      `[login] Credential form not found after completing slug step. ` +
      `Current URL: ${page.url()}. The login page layout may have changed.`,
    )
  }

  await emailInput.fill(env.credentials.admin.email)
  await passwordInput.fill(env.credentials.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60_000 })
}
