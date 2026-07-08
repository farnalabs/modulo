import { test, expect, loginAsAdmin } from './setup/fixtures'

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

  test('shows error on failed login', async ({ page }) => {
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await page.fill('input[type="text"]', 'wrong@example.com')
    await page.fill('input[type="password"]', 'wrongpassword')
    await page.click('button[type="submit"]')

    await expect(page.locator('text=Login failed')).toBeVisible({ timeout: 5000 })
  })

  test('redirects away from login on successful login', async ({ page, env }) => {
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await loginAsAdmin(page, env)

    await expect(page).not.toHaveURL(/\/login/)
  })
})
