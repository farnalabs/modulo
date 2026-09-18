import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Housekeeping', { tag: "@regression" }, () => {
  // FAR-938 gates the SYSTEM sidebar group (and every route in it) on the
  // is_system_admin JWT claim. No e2e identity holds that claim, so the
  // router guard redirects this route to the dashboard. Rendering is covered
  // by AdminHousekeepingView.spec.ts at the unit level.
  test('redirects a non-system-admin away from the Housekeeping page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/housekeeping')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('h1')).toContainText('Dashboard')
  })
})
