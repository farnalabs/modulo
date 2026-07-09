import { test, expect } from './setup/fixtures'

test.describe('Login Flow', () => {
  test('shows login form fields', async ({ page }) => {
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Modulo')

    const emailInput = page.locator('input[type="text"]')
    await expect(emailInput).toBeVisible()
    await expect(emailInput).toHaveAttribute('placeholder', /admin@example\.com/)

    const passwordInput = page.locator('input[type="password"]')
    await expect(passwordInput).toBeVisible()

    await expect(page.locator('button[type="submit"]')).toContainText('Sign in')
  })

  test('shows error on failed login', { tag: '@smoke' }, async ({ page }) => {
    await page.route('**/api/v1/auth/login', async (route) => {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Invalid credentials' }) })
    })

    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await page.fill('input[type="text"]', 'wrong@example.com')
    await page.fill('input[type="password"]', 'wrongpassword')
    await page.click('button[type="submit"]')

    await expect(page.locator('text=Invalid credentials')).toBeVisible({ timeout: 5000 })
  })

  test('redirects away from login on successful login', { tag: '@smoke' }, async ({ page }) => {
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

    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await page.fill('input[type="text"]', 'admin@example.com')
    await page.fill('input[type="password"]', 'password123')
    await page.click('button[type="submit"]')

    await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 10000 })
    await expect(page.getByTestId('dashboard-title')).toContainText('Dashboard')
  })
})
