import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Feature Flags', () => {
  test('feature flags page loads', async ({ page }) => {
    await login(page)
    await page.goto('/admin/feature-flags')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Feature Flag/i)
  })
})
