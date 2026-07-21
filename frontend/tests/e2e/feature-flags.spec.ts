import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Feature Flags', () => {
  test('feature flags page loads', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/feature-flags')

    await expect(page.locator('h1')).toContainText(/Feature Flag/i)
  })
})
