import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

const sampleCostData = {
  totalSpend: 1234.56,
  avgCostPerRun: 2.34,
  totalRuns: 528,
  items: [],
}

test.describe('Admin Cost Breakdown', () => {
  test('page loads with correct heading', async ({ page }) => {
    await page.route('**/api/v1/costs*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleCostData) })
    })
    await login(page)

    await page.goto('/admin/costs')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Cost Breakdown')
  })

  test('shows cost summary cards', async ({ page }) => {
    await page.route('**/api/v1/costs*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleCostData) })
    })
    await login(page)

    await page.goto('/admin/costs')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('cost-total-spend')).toBeVisible()
    await expect(page.getByTestId('cost-avg-per-run')).toBeVisible()
    await expect(page.getByTestId('cost-total-runs')).toBeVisible()
  })
})
