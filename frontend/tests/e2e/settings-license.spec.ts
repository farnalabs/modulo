import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings License', { tag: "@regression" }, () => {
  // FAR-938 gates /settings/license (SYSTEM sidebar group) on the
  // is_system_admin JWT claim. No e2e identity holds that claim, so the router
  // guard redirects to the dashboard. Rendering is covered by
  // SettingsLicenseView.spec.ts at the unit level.
  test('redirects a non-system-admin away from the License page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/license')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('h1')).toContainText('Dashboard')
  })
})

test.describe('Settings SSO', { tag: "@regression" }, () => {
  test('renders the SSO page', { tag: "@regression" }, async ({ page, env }) => {
    await page.route('**/api/v1/sso*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'sso1', provider: 'google', domain: 'example.com', enabled: true, created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/settings/sso')
    await expect(page.locator('h1')).toContainText('SSO')
    await expect(page.getByTestId('settings-sso-add-provider')).toBeVisible()
  })
})
