import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Run Retention', { tag: "@regression" }, () => {
  test('renders the Run Retention page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')
    await expect(page.locator('h1')).toContainText('Run Retention')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-run-retention-refresh')).toBeVisible()
    }
  })
})
