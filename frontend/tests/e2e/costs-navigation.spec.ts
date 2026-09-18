import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * Regression test for FAR-852: a view with multiple root elements breaks
 * AppLayout's <transition name="page" mode="out-in">, leaving the NEXT page
 * blank after a client-side navigation.
 *
 * The weak original assertion (page.textContent('body').length > 0) passed even
 * when the routed page was blank, because the app shell (sidebar/nav) still
 * renders. Here we instead navigate away from each Costs page via a real
 * client-side sidebar link and assert the destination's heading actually
 * renders — which is exactly what the multi-root bug broke.
 */
const costRoutes = [
  { path: '/admin/costs', heading: 'Cost Breakdown' },
  { path: '/admin/costs/limits', heading: 'Spend Limits' },
  { path: '/admin/costs/components', heading: 'Cost Components' },
  { path: '/admin/costs/controls', heading: 'Cost Controls' },
]

test.describe('Costs page navigation does not break rendering', { tag: '@regression' }, () => {
  for (const { path, heading } of costRoutes) {
    test(`navigating away from ${path} renders the next page`, { tag: '@regression' }, async ({ page, env }) => {
      await loginAsAdmin(page, env)

      // The Costs page itself must render its heading (single-root view).
      await page.goto(path)
      await expect(
        page.getByRole('heading', { level: 1, name: heading }),
      ).toBeVisible({ timeout: 10000 })

      // Navigate away with a real client-side transition — this is the path
      // that FAR-852 broke: leaving a multi-root view left the next page blank.
      const dashboardLink = page.locator('a.sidebar-link', { hasText: 'Dashboard' }).first()
      await expect(dashboardLink).toBeVisible({ timeout: 10000 })
      await dashboardLink.click()

      await expect(page).toHaveURL(/\/$/)
      // The destination heading must actually render — not just the app shell.
      await expect(page.getByTestId('dashboard-title')).toBeVisible({ timeout: 10000 })
    })
  }
})
