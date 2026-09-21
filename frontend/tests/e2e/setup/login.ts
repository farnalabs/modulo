import { type Page } from '@playwright/test'
import type { TestEnv } from './env'
import { resolveLoginPath } from './login-path'
import { getBaseUrl } from './env'

/**
 * Sign in through the real login UI for the staging/app targets.
 *
 * Uses resolveLoginPath() to query /api/v1/auth/login-context at runtime and
 * determine the correct login URL based on the instance's multi-org state.
 * Falls back to /login (safe on both single-org and multi-org) when the
 * login-context fetch fails, then completeLoginForm handles whichever UI
 * the instance renders (email form or slug selector).
 */
export async function loginThroughUi(page: Page, env: TestEnv): Promise<void> {
  const baseURL = getBaseUrl(env.name)
  const loginPath = await resolveLoginPath(baseURL)

  await page.goto(baseURL + loginPath)

  // Fail-fast: detect a missing login form with a short initial wait, then
  // throw a clear diagnostic instead of a bare 30s selector timeout.
  const emailSelector = env.credentials.loginFormEmailSelector
  const formVisible = await page
    .locator(emailSelector)
    .waitFor({ state: 'visible', timeout: 5000 })
    .then(() => true)
    .catch(() => false)

  if (!formVisible) {
    // Capture diagnostic context before throwing
    const currentURL = page.url()
    const pageTitle = await page.title()
    const bodyText = await page.locator('body').innerText().catch(() => '<unreadable>')
    const screenshot = await page.screenshot().catch(() => null)

    const diagnostic = [
      `[login] Login form not found at ${currentURL}`,
      `[login] Expected selector: ${emailSelector}`,
      `[login] Page title: ${pageTitle}`,
      `[login] Page body (first 500 chars): ${bodyText.slice(0, 500)}`,
      `[login] loginPath resolved to: ${loginPath}`,
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

  // Form is visible — fill and submit
  await page.fill(emailSelector, env.credentials.admin.email)
  await page.fill(env.credentials.loginFormPasswordSelector, env.credentials.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60000 })
}
