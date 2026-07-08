import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Remy Admin Configuration', () => {
  test('page loads with remy configuration sections', async ({ page }) => {
    await login(page)

    await page.goto('/admin/remy')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Remy Configuration')
    await expect(page.getByTestId('remy-providers')).toBeVisible()
    await expect(page.getByTestId('remy-custom-backends')).toBeVisible()
  })
})
