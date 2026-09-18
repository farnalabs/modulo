import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('Settings Email', { tag: "@regression" }, () => {
  // FAR-938/FAR-970 gate /settings/email (SYSTEM sidebar group) on the
  // is_system_admin JWT claim. No e2e identity holds that claim, so the router
  // guard redirects to the dashboard. Rendering is covered by
  // SettingsEmailView.spec.ts at the unit level.
  test('redirects a non-system-admin away from the Email Settings page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/email')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('h1')).toContainText('Dashboard')
  })
})

test.describe('Settings Error Forwarders', { tag: "@regression" }, () => {
  test('renders the Error Forwarders page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/settings/error-forwarders')
    await expect(page.locator('h1')).toContainText('Error Forwarders')
  })
})

test.describe('Settings Observability', { tag: "@regression" }, () => {
  // FAR-938 gates the SYSTEM sidebar group on is_system_admin; with no such
  // e2e identity the router guard redirects to the dashboard. Rendering is
  // covered by SettingsObservabilityView.spec.ts at the unit level.
  test('redirects a non-system-admin away from the Observability page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/observability')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('h1')).toContainText('Dashboard')
  })
})
