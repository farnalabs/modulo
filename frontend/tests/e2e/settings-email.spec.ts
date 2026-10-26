import { test, expect, loginAsAdmin, loginForSystemRoute, isDevModeTarget } from './setup/fixtures'

test.describe('Settings Email', { tag: "@regression" }, () => {
  test('renders the Email Settings page', { tag: "@regression" }, async ({ page, env }) => {
    // Email delivery is organisation-scoped (the API lives under
    // /api/v1/admin/org/{org_id}), so /settings/email lives in the ADMIN
    // sidebar group behind required_roles: [admin] (#2455 moved it out of the
    // system-admin-only SYSTEM group). The staging/prod e2e identity is a
    // regular org admin and therefore reaches the page;
    // loginForSystemRoute's extra is_system_admin claim is no longer needed
    // here.
    await loginAsAdmin(page, env)
    if (env.name === 'local') {
      // Registered after login so it is not shadowed by setupLocalMockApi's
      // catch-all (Playwright matches the last registered route first).
      await page.route('**/api/v1/email/config*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ smtp_host: 'smtp.example.com', smtp_port: 587, from_address: 'noreply@example.com', encryption: 'tls', enabled: true }) })
      })
    }
    await page.goto('/settings/email')
    await expect(page).toHaveURL(/\/settings\/email$/)
    await expect(page.locator('h1')).toContainText('Email Settings')
  })
})

test.describe('Settings Error Forwarders', { tag: "@regression" }, () => {
  test('renders the Error Forwarders page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginForSystemRoute(page, env)
    await page.goto('/settings/error-forwarders')
    await expect(page.locator('h1')).toContainText('Error Forwarders')
  })
})

test.describe('Settings Observability', { tag: "@regression" }, () => {
  test('renders the Observability page', { tag: "@regression" }, async ({ page, env }) => {
    const systemAdmin = await loginForSystemRoute(page, env)
    await page.goto('/settings/observability')
    if (!systemAdmin) {
      // FAR-938: the SYSTEM sidebar group is system-admin-only. The staging/prod
      // e2e identity is a regular org admin, so the router guard redirects to '/'.
      await expect(page).toHaveURL(/\/$/)
      await expect(page.locator('h1')).toContainText('Dashboard')
      return
    }
    await expect(page.locator('h1')).toContainText('Observability')
    if (env.name === 'local') {
      await expect(page.getByTestId('settings-observability-otlp-endpoint')).toBeVisible()
    }
  })
})
