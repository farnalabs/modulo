import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings License', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/license')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('License')
    await expect(page.getByTestId('license-title')).toBeVisible()
  })
})

test.describe('Settings SSO', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/sso')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('SSO')
    await expect(page.getByTestId('settings-sso-add-provider')).toBeVisible()
  })
})
