import { test, expect, loginAsAdmin } from './setup/fixtures'

const sampleCostData = {
  totalSpend: 1234.56,
  avgCostPerRun: 2.34,
  totalRuns: 528,
  items: [],
}

test.describe('Admin Cost Breakdown', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await page.route('**/api/v1/costs*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleCostData) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/costs')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Cost Breakdown')
  })

  test('shows cost summary cards', async ({ page, env }) => {
    await page.route('**/api/v1/costs*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleCostData) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/costs')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('cost-total-spend')).toBeVisible()
    await expect(page.getByTestId('cost-avg-per-run')).toBeVisible()
    await expect(page.getByTestId('cost-total-runs')).toBeVisible()
  })
})
