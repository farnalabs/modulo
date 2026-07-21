import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('AB Test Models', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/variants/ab-test')
    await expect(page.locator('h1')).toContainText('A/B Test Models')
    await expect(page.getByTestId('ab-test-models-pipeline-select')).toBeVisible()
  })
})
