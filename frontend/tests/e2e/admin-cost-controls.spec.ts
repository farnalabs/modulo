import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Spend Limits', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await page.route('**/api/v1/costs/limits*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ limits: [] }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/limits')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Spend Limits')
    await expect(page.getByTestId('admin-spend-limits-org-limit')).toBeVisible()
  })
})

test.describe('Admin Cost Controls', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/controls')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Cost Controls')
    await expect(page.getByTestId('cc-total-spend')).toBeVisible()
  })
})
