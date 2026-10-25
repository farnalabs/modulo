import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings License', { tag: "@regression" }, () => {
  test('renders the License page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    if (env.name === 'local') {
      // Registered after login so they are not shadowed by setupLocalMockApi's
      // catch-all (Playwright matches the last registered route first).
      //
      // FAR-2104: /settings/license sits in the ADMIN group behind the
      // `org.license.manage` permission (an org-admin permission, no longer the
      // FAR-938 SYSTEM group). The local mock's catch-all answers the
      // caller-scoped GET /api/v1/auth/permissions with `{items: [], total: 0}`
      // — no `permissions` array — which leaves the router's permission gate
      // fail-closed and bounces the page to the dashboard. Supply the same
      // permission the staging e2e org admin holds, so both targets exercise
      // the shipped gate instead of a mock gap.
      await page.route('**/api/v1/auth/permissions', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ permissions: ['org.license.manage'] }) })
      })
      await page.route('**/api/v1/admin/license*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_license: true, tier: 'team', features: ['sso', 'audit_log', 'custom_roles'], expires_at: '2026-06-01T10:00:00Z', org_id: 'org1' }) })
      })
    }
    await page.goto('/settings/license')
    // FAR-2104: the licence is an org-level object, so an org admin — the
    // staging/prod e2e identity — reaches this screen and it renders. The old
    // FAR-938 SYSTEM-group assertion (redirect to the dashboard) is stale.
    await expect(page).toHaveURL(/\/settings\/license$/)
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
