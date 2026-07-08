import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Copy Pipeline', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/pipelines/copy')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Copy Pipeline')
    await expect(page.getByTestId('page-pipeline-copy')).toBeVisible()
  })
})
