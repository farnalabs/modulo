import { type Page } from '@playwright/test'
import type { TestEnv } from './env'
import { getBaseUrl } from './env'
import { resolveLoginPath } from './login-path'
import { completeLoginForm } from './fixtures'

/**
 * Sign in through the real login UI for the staging/app targets.
 *
 * Uses resolveLoginPath() to query /api/v1/auth/login-context at runtime to
 * pick the correct login URL for the instance's multi-org state, falling back
 * to /login (safe on both single-org and multi-org) when the fetch fails.
 * completeLoginForm() then completes the multi-org slug step if one rendered,
 * guarantees the credential form is on screen, and fails fast with a
 * diagnostic if it is not — so this path never sits on a bare 30 s selector
 * timeout.
 */
export async function loginThroughUi(page: Page, env: TestEnv): Promise<void> {
  const baseURL = getBaseUrl(env.name)
  const loginPath = await resolveLoginPath(baseURL)

  await page.goto(baseURL + loginPath)

  const form = await completeLoginForm(page, env)

  await page.fill(form.email, env.credentials.admin.email)
  await page.fill(form.password, env.credentials.admin.password)
  await page.click(form.submit)
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60_000 })
}
