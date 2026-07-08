import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Settings License', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/license')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('License')
    await expect(page.getByTestId('page-settings-license')).toBeVisible()
  })
})

test.describe('Settings SSO', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/sso')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('SSO')
    await expect(page.getByTestId('page-settings-sso')).toBeVisible()
  })
})
