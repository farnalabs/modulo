import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings License', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/admin/license*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_license: true, tier: 'team', features: ['sso', 'audit_log', 'custom_roles'], expires_at: '2026-06-01T10:00:00Z', org_id: 'org1' }) })
    })
    await page.goto('/settings/license')
    await expect(page.locator('h1')).toContainText('License')
    await expect(page.getByTestId('license-title')).toBeVisible()
  })
})

test.describe('Settings SSO', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    await page.route('**/api/v1/sso*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'sso1', provider: 'google', domain: 'example.com', enabled: true, created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/settings/sso')
    await expect(page.locator('h1')).toContainText('SSO')
    await expect(page.getByTestId('settings-sso-add-provider')).toBeVisible()
  })
})
