import { type Page } from '@playwright/test'
import type { TestEnv } from './env'
import { resolveLoginPath } from './login-path'
import { getBaseUrl } from './env'
import { completeLoginForm } from './fixtures'

/**
 * Sign in through the real login UI for the staging/app targets.
 *
 * Uses resolveLoginPath() to query /api/v1/auth/login-context at runtime and
 * determine the correct login URL based on the instance's multi-org state.
 * Falls back to /login (safe on both single-org and multi-org) when the
 * login-context fetch fails.
 *
 * Multi-org instances render an org-slug entry step at /login before the
 * email/password form.  Single-org instances render the credential form
 * directly.  The slug step is detected and completed automatically by
 * completeLoginForm(); callers never need to know which path the instance
 * takes.
 */
export async function loginThroughUi(page: Page, env: TestEnv): Promise<void> {
  const baseURL = getBaseUrl(env.name)
  const loginPath = await resolveLoginPath(baseURL)

  await page.goto(baseURL + loginPath)

  // Handles the multi-org slug step (if present) and guarantees the credential
  // form is on screen, failing fast with a diagnostic if it is not. The
  // returned selectors match whichever layout rendered (LoginView on
  // single-org, OrgLoginView on multi-org).
  const form = await completeLoginForm(page, env)

  await page.fill(form.email, env.credentials.admin.email)
  await page.fill(form.password, env.credentials.admin.password)
  await page.click(form.submit)
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60_000 })
}
