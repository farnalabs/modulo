import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

// Craft a real JWT (header.payload.signature) whose payload carries
// org_role: 'admin' so the admin-role sidebar filter shows /admin/remy.
function b64url(input: unknown): string {
  return Buffer.from(JSON.stringify(input)).toString('base64url')
}

function adminJwt(): string {
  const header = b64url({ alg: 'HS256', typ: 'JWT' })
  const now = Math.floor(Date.now() / 1000)
  const payload = b64url({
    sub: '1',
    email: 'admin@example.com',
    name: 'Admin',
    org_role: 'admin',
    is_system_admin: true,
    iat: now,
    exp: now + 3600,
  })
  return `${header}.${payload}.mocked-signature`
}

test.describe('Remy-only mode at /remy', { tag: '@smoke' }, () => {
  test('sidebar shows the dev-mode /admin/remy control and hides /remy; /remy renders behind auth', { tag: '@smoke' }, async ({ page, env }) => {
    test.setTimeout(120_000)
    test.skip(!isDevModeTarget(env), 'Remy is dev-mode-gated (private_preview); only runs on a dev-mode target')

    await loginAsAdmin(page, env)

    // Register the dev_mode flag AFTER loginAsAdmin: setupLocalMockApi
    // installs a catch-all **/api/v1/** route, and Playwright matches the
    // LAST-registered route first, so a feature-flags stub registered before
    // it would be shadowed.
    await page.route('**/api/v1/admin/feature-flags', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          flags: [],
          license: { tier: 'enterprise' },
          dev_mode: true,
        }),
      })
    })

    // The mock login token is not a JWT — swap in one with org_role 'admin'
    // so the admin-role sidebar filter keeps /admin/remy visible.
    await page.evaluate((token) => {
      localStorage.setItem('modulo_access_token', token)
    }, adminJwt())

    await page.goto('/')
    await expect(page.locator('button[aria-controls="sidebar-group-admin"]').first()).toBeVisible({ timeout: 30000 })

    // Positive control: the private_preview /admin/remy item IS in the sidebar
    // when dev mode is on. The admin group starts collapsed — expand it.
    await page.click('button[aria-controls="sidebar-group-admin"] >> nth=0')
    await expect(page.locator('a[href="/admin/remy"]').first()).toBeVisible()

    // Negative control: /remy is a bare route, never listed in the sidebar.
    await expect(page.locator('a[href="/remy"]')).toHaveCount(0)

    // Direct navigation to /remy renders the view behind the auth guard
    // (dev mode is on, so the private_preview guard passes).
    await page.goto('/remy')
    await expect(page.locator('[data-testid="remy-only-view"]')).toBeVisible()
    await expect(page.locator('[data-testid="remy-only-banner"]')).toBeVisible()
  })
})
