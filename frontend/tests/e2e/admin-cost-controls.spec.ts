import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Admin Spend Limits', () => {
  test('page loads with correct heading', async ({ page }) => {
    await page.route('**/api/v1/costs/limits*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ limits: [] }) })
    })
    await login(page)
    await page.goto('/admin/costs/limits')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Spend Limits')
    await expect(page.getByTestId('page-spend-limits')).toBeVisible()
  })
})

test.describe('Admin Cost Controls', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/costs/controls')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Cost Controls')
    await expect(page.getByTestId('page-cost-controls')).toBeVisible()
  })
})
