import { test, expect, loginAsAdmin, loginForSystemRoute } from './setup/fixtures'

test.describe('Settings License', { tag: "@regression" }, () => {
  test('renders the License page', { tag: "@regression" }, async ({ page, env }) => {
    const systemAdmin = await loginForSystemRoute(page, env)
    if (env.name === 'local') {
      // Registered after login so it is not shadowed by setupLocalMockApi's
      // catch-all (Playwright matches the last registered route first).
      await page.route('**/api/v1/admin/license*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_license: true, tier: 'team', features: ['sso', 'audit_log', 'custom_roles'], expires_at: '2026-06-01T10:00:00Z', org_id: 'org1' }) })
      })
    }
    await page.goto('/settings/license')
    if (!systemAdmin) {
      // FAR-938: the SYSTEM sidebar group is system-admin-only. The staging/prod
      // e2e identity is a regular org admin, so the router guard redirects to '/'.
      await expect(page).toHaveURL(/\/$/)
      await expect(page.locator('h1')).toContainText('Dashboard')
      return
    }
    await expect(page.locator('h1')).toContainText('License')
    await expect(page.getByTestId('license-title')).toBeVisible()
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
