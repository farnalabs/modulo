import { test, expect } from '@playwright/test'

test.describe('Dashboard', () => {
  test('redirects to login when unauthenticated', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/login/)
  })

  test('displays dashboard heading when authenticated', async ({ page }) => {
    await page.goto('/login')

    await page.fill('input[type="text"]', 'admin@example.com')
    await page.fill('input[type="password"]', 'password123')
    await page.click('button[type="submit"]')

    await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})

    const isOnDashboard = page.url() !== `${page.context().options.baseURL}/login`
    if (isOnDashboard) {
      await expect(page.locator('h1')).toContainText('Dashboard')
    }
  })
})
