import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Output Diff', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/runs/diff')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Output Diff')
    await expect(page.getByTestId('page-output-diff')).toBeVisible()
  })
})
