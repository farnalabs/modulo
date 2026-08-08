import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('AB Test Models', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/variants/ab-test')
    await expect(page.locator('h1')).toContainText('A/B Test Models')
    await expect(page.getByTestId('ab-test-models-pipeline-select')).toBeVisible()
  })
})
