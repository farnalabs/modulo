import { test, expect, loginAsAdmin } from './setup/fixtures'

const sampleCostData = {
  totalSpend: 1234.56,
  avgCostPerRun: 2.34,
  totalRuns: 528,
  items: [
    { category: 'LLM Calls', amount: 800.00, percentage: 64.8, run_count: 320 },
    { category: 'Embeddings', amount: 250.00, percentage: 20.2, run_count: 150 },
    { category: 'Storage', amount: 184.56, percentage: 15.0, run_count: 58 },
  ],
}

test.describe('Admin Cost Breakdown', { tag: "@regression" }, () => {
  test('renders the Cost Breakdown page', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/costs*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleCostData) })
      })
    }
    await loginAsAdmin(page, env)

    await page.goto('/admin/costs')

    await expect(page.locator('h1')).toContainText('Cost Breakdown')
  })

  test('shows cost summary cards', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/costs*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleCostData) })
      })
    }
    await loginAsAdmin(page, env)

    await page.goto('/admin/costs')

    await expect(page.locator('h1')).toContainText('Cost Breakdown')
    if (env.name === 'local') {
      await expect(page.getByTestId('cost-total-spend')).toBeVisible()
      await expect(page.getByTestId('cost-avg-per-run')).toBeVisible()
      await expect(page.getByTestId('cost-total-runs')).toBeVisible()
    }
  })
})
