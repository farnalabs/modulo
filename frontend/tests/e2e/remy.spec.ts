import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Remy Admin Configuration', () => {
  test('page loads with remy configuration sections', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/admin/remy')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Remy Configuration')
    await expect(page.getByTestId('remy-providers')).toBeVisible()
    await expect(page.getByTestId('remy-custom-backends')).toBeVisible()
  })
})
