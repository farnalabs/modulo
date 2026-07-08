import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings Email', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/email')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Email')
    await expect(page.getByTestId('page-settings-email')).toBeVisible()
  })
})

test.describe('Settings Error Forwarders', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/error-forwarders')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Error Forwarders')
    await expect(page.getByTestId('page-error-forwarders')).toBeVisible()
  })
})

test.describe('Settings Observability', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/observability')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Observability')
    await expect(page.getByTestId('page-settings-observability')).toBeVisible()
  })
})
