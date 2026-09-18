import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Run Retention', { tag: "@regression" }, () => {
  // FAR-938 gates the SYSTEM sidebar group (and every route in it) on the
  // is_system_admin JWT claim. No e2e identity holds that claim, so the
  // router guard redirects this route to the dashboard. Rendering and the
  // FAR-868 auto-filter behaviour are covered by AdminRunRetentionView.spec.ts
  // at the unit level.
  test('redirects a non-system-admin away from the Run Retention page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('h1')).toContainText('Dashboard')
  })

  test('exposes no FAR-868 auto-filter controls without system admin', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')

    // The route is gated, so its filter controls never render. In particular
    // there is no Apply Filters button (FAR-868 auto-applies filters).
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByTestId('admin-run-retention-apply')).toHaveCount(0)
    await expect(page.getByTestId('admin-run-retention-reset')).toHaveCount(0)
  })
})
