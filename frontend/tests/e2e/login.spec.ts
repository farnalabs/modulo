import { test, expect } from './setup/fixtures'

test.describe('Login Flow', () => {
  test('shows login form fields', { tag: "@regression" }, async ({ page }) => {
    await page.goto('/login', { timeout: 60000 })

    await expect(page.locator('h1')).toContainText('Modulo')

    const emailInput = page.locator('input[type="text"]')
    await expect(emailInput).toBeVisible()
    await expect(emailInput).toHaveAttribute('placeholder', /admin@example\.com/)

    const passwordInput = page.locator('input[type="password"]')
    await expect(passwordInput).toBeVisible()

    await expect(page.locator('button[type="submit"]')).toContainText('Sign in')
  })

  test('shows error on failed login', { tag: '@smoke' }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/auth/login', async (route) => {
        await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Invalid credentials' }) })
      })
    }

    await page.goto('/login')

    await page.fill(env.credentials.loginFormEmailSelector, 'wrong@example.com')
    await page.fill(env.credentials.loginFormPasswordSelector, 'thisiswrong')
    await page.click('button[type="submit"]')

    await expect(page.getByText(/Invalid credentials|Incorrect email or password/)).toBeVisible({ timeout: 15000 })
  })

  test('surfaces configured SSO providers as login buttons', { tag: '@regression' }, async ({ page }) => {
    await page.route('**/api/v1/auth/sso/providers', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ oidc: [{ provider_id: 'google' }], saml: true }),
      })
    })

    await page.goto('/login')

    await expect(page.getByTestId('login-sso-section')).toBeVisible()
    await expect(page.getByTestId('login-sso-oidc-google')).toHaveAttribute(
      'href',
      '/api/v1/auth/oidc/google/login',
    )
    await expect(page.getByTestId('login-sso-saml')).toHaveAttribute('href', '/api/v1/auth/saml/login')
  })

  test('keeps password login when no SSO providers are configured', { tag: '@regression' }, async ({ page }) => {
    await page.route('**/api/v1/auth/sso/providers', (route) => {
      void route.fulfill({
        status: 402,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'This feature is not available on your plan' }),
      })
    })

    await page.goto('/login')

    await expect(page.getByTestId('login-sso-section')).toHaveCount(0)
    await expect(page.getByTestId('login-submit')).toBeVisible()
  })

  test('redirects away from login on successful login', { tag: '@smoke' }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/auth/login', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            access_token: 'real-login-flow-token',
            refresh_token: 'real-login-flow-refresh-token',
            token_type: 'bearer',
          }),
        })
      })
      await page.route('**/api/v1/me/settings', async (route) => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ locale: 'en-US' }) })
      })
    }

    await page.goto('/login')

    await page.fill(env.credentials.loginFormEmailSelector, env.credentials.admin.email)
    await page.fill(env.credentials.loginFormPasswordSelector, env.credentials.admin.password)
    await page.click('button[type="submit"]')

    await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 15000 })
    await expect(page.getByTestId('dashboard-title')).toContainText('Dashboard')
  })
})
