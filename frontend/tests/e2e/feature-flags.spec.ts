import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('Feature Flags', { tag: "@regression" }, () => {
  test('feature flags page loads', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/admin/feature-flags')

    await expect(page.locator('h1')).toContainText(/Feature Flag/i)
  })
})
