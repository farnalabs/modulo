import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

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
  test('renders the Webhook Notifications page by default', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/notification-delivery')
    await expect(page.locator('h1')).toContainText('Webhook Notifications')
    await expect(page.getByTestId('admin-notification-log-title')).toBeVisible()
  })

  test('redirects to the dashboard when an explicit feature override disables the flag', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/plan*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ license: { tier: 'team' }, flags: [{ name: 'webhook_notification_log', currently_active: false }] }) })
    })
    await page.reload()
    await page.goto('/admin/notification-delivery')
    await expect(page).toHaveURL(/\/$/)
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
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/plan*', (route) => {
      // `tier` must use the live vocabulary (community/team/enterprise): the
      // retired 'v2' bucket is unknown to the plan store, whose tier-derived
      // computeds then throw during router startup
      // (`VUE_ROUTER_R0011 Unexpected error when starting the router`) and the
      // app never mounts. `dev_mode: true` is also required: /admin/plugins is
      // private_preview, so without it the guard redirects to the dashboard.
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ license: { tier: 'team' }, dev_mode: true, flags: [{ name: 'plugin_management', currently_active: true }, { name: 'model_backend_management', currently_active: true }, { name: 'team_rbac', currently_active: true }] }) })
    })
    await page.route('**/api/v1/plugins*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ PLUGIN_ID: 'p1', display_name: 'Test Plugin', description: 'A test plugin', version: '1.0.0', capabilities: ['connector_type'], health_ok: true, health_detail: 'OK', health_checked_at: '2025-06-01T12:00:00Z' }]) })
    })
    // Fresh plan store so the guard re-reads the mocked plan (see the
    // notification-delivery test above for why a reload is required).
    await page.reload()
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

test.describe('Admin Trigger Event Log', { tag: "@regression" }, () => {
  test('renders the org-wide Trigger Event Log page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/trigger-events')
    await expect(page).toHaveURL(/\/admin\/trigger-events$/)
    await expect(page.locator('h1')).toContainText('Trigger Event Log')
    await expect(page.getByTestId('settings-trigger-event-log-trigger-type')).toBeVisible()
    await expect(page.getByTestId('settings-trigger-event-log-result')).toBeVisible()
  })
})
