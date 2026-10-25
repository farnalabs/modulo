import { test, expect, loginAsAdmin, systemAdminJwt } from './setup/fixtures'

test.describe('Settings License', { tag: "@regression" }, () => {
  test('renders the License page', { tag: "@regression" }, async ({ page, env }) => {
    // FAR-938 made the whole SYSTEM sidebar group system-admin-only, so this
    // spec used to expect the staging/prod org-admin identity to be bounced to
    // '/'. FAR-2104 changed that contract for THIS route only: /settings/license
    // moved to the ADMIN group with `required_permissions: [org.license.manage]`
    // (held by the admin org role) and a community tier floor, so an org admin
    // reaches it and acts on the org-scoped controls. Every OTHER SYSTEM route
    // is still system-admin-only — the contrast check below proves that gate
    // still bites, so this test cannot decay into "everything is reachable".
    //
    // `loginForSystemRoute` is deliberately NOT used here: its boolean means
    // "this principal can render a SYSTEM-group route", and the licence is no
    // longer one. On staging it would report false for a page an org admin can
    // now open. The licence assertions run on the ordinary org-admin session
    // (`loginAsAdmin`) — the identity that used to be redirected.
    await loginAsAdmin(page, env)

    if (env.name === 'local') {
      // Registered after login so they are not shadowed by setupLocalMockApi's
      // catch-all (Playwright matches the last registered route first). The
      // mock permission read answers `{}` in the catch-all, which would hide a
      // `required_permissions` route even from an admin.
      await page.route('**/api/v1/auth/permissions*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ permissions: ['org.license.manage'] }) })
      })
      await page.route('**/api/v1/admin/license*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_license: true, tier: 'team', features: ['sso', 'audit_log', 'custom_roles'], expires_at: '2026-06-01T10:00:00Z', org_id: 'org1' }) })
      })
    }

    // Contrast (FAR-938 still holds outside the licence): /admin/housekeeping is
    // in the SYSTEM group with no extra role/tier/flag gate, so this org-admin
    // session is redirected by the system-admin guard alone.
    await page.goto('/admin/housekeeping')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('h1')).toContainText('Dashboard')

    // FAR-2104: the org admin now lands on the licence screen and sees the
    // org-scoped controls it may act on. The instance-wide controls are
    // system-admin-only and must NOT render for them.
    await page.goto('/settings/license')
    await expect(page).toHaveURL(/\/settings\/license$/)
    await expect(page.locator('h1')).toContainText('License')
    await expect(page.getByTestId('license-title')).toBeVisible()
    await expect(page.getByTestId('licence-org-controls')).toBeVisible()
    await expect(page.getByTestId('licence-system-controls')).toHaveCount(0)

    if (env.name === 'local') {
      // A system admin still reaches the whole licence screen. Only the local
      // mock target can mint an is_system_admin JWT: the staging/prod e2e
      // identity is a regular org admin and there is no in-product grant.
      await page.unroute('**/api/v1/auth/permissions*')
      await page.route('**/api/v1/auth/permissions*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ permissions: ['org.license.manage', 'system.config.manage'] }) })
      })
      await page.evaluate((token) => {
        localStorage.setItem('modulo_access_token', token)
      }, systemAdminJwt())
      await page.goto('/settings/license')
      await expect(page.locator('h1')).toContainText('License')
      await expect(page.getByTestId('license-title')).toBeVisible()
      await expect(page.getByTestId('licence-system-controls')).toBeVisible()
    }
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
