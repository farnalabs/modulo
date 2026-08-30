import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Audit Log', { tag: "@regression" }, () => {
  test('renders the Audit Log page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/audit')
    await expect(page.locator('h1')).toContainText('Audit Log')
    await expect(page.getByTestId('admin-audit-verify-chain')).toBeVisible()
  })
})

test.describe('Admin My Profile', { tag: "@regression" }, () => {
  test('renders the My Profile page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/my-profile')
    await expect(page.locator('h1')).toContainText('My Profile')
    await expect(page.getByTestId('change-password-current')).toBeVisible()
  })
})

test.describe('Admin Notification Delivery', { tag: "@regression" }, () => {
  test('renders the Notification Delivery page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/notification-delivery')
    await expect(page.locator('h1')).toContainText('Notification Delivery Log')
    await expect(page.getByTestId('admin-notification-log-title')).toBeVisible()
  })
})

test.describe('Admin Org Settings', { tag: "@regression" }, () => {
  test('renders the Org Settings page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/admin/billing/overview*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ total_users: 5, total_teams: 2, total_pipelines: 10, plan_tier: 'team', plan_id: 'plan_1' }) })
    })
    await page.route('**/api/v1/admin/org/export*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ organisation: { id: 'org1', name: 'Test Org', slug: 'test-org', created_at: '2025-01-01T00:00:00Z' }, exported_at: '2025-06-01T12:00:00Z' }) })
    })
    await page.goto('/admin/org')
    await expect(page.locator('h1')).toContainText('Organisation Settings')
    await expect(page.locator('text=Organisation Info')).toBeVisible()
  })
})

test.describe('Admin Pipelines', { tag: "@regression" }, () => {
  test('renders the Pipelines page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/pipelines')
    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-new-pipeline')).toBeVisible()
  })
})

test.describe('Admin Plugins', { tag: "@regression" }, () => {
  test('renders the Plugins page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/admin/feature-flags*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ license: { tier: 'v2' }, flags: [{ name: 'plugin_management', currently_active: true }, { name: 'model_backend_management', currently_active: true }, { name: 'team_rbac', currently_active: true }] }) })
    })
    await page.route('**/api/v1/plugins*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ PLUGIN_ID: 'p1', display_name: 'Test Plugin', description: 'A test plugin', version: '1.0.0', capabilities: ['connector_type'], health_ok: true, health_detail: 'OK', health_checked_at: '2025-06-01T12:00:00Z' }]) })
    })
    await page.goto('/admin/plugins')
    await expect(page.locator('h1')).toContainText('Plugins')
    await expect(page.getByTestId('admin-plugins-refresh')).toBeVisible()
  })
})

test.describe('Admin Triggers', { tag: "@regression" }, () => {
  test('renders the Triggers page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/triggers')
    await expect(page.locator('h1')).toContainText('Triggers')
    await expect(page.getByTestId('settings-triggers-create')).toBeVisible()
  })
})
