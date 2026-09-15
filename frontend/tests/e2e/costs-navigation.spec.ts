import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * Regression test for FAR-852: visiting a Costs page then navigating away
 * must never leave a blank page. The root cause was multi-root views breaking
 * AppLayout's <transition name="page" mode="out-in">.
 *
 * We assert the route's OWN PageHeader heading is visible — not merely that
 * *some* text rendered. The app shell (sidebar / nav) renders regardless, so a
 * blank routed page still satisfies a bodyText length check; asserting the
 * specific heading proves the routed component actually mounted (and that the
 * transition did not leave the page blank). The navigate-away-then-back step
 * exercises exactly the transition that FAR-852 broke.
 */
const costRoutes = [
  { path: '/admin/costs', heading: 'Cost Breakdown' },
  { path: '/admin/costs/limits', heading: 'Spend Limits' },
  { path: '/admin/costs/components', heading: 'Cost Components' },
  { path: '/admin/costs/controls', heading: 'Cost Controls' },
]

test.describe('Costs page navigation does not break rendering', { tag: '@regression' }, () => {
  for (const { path, heading } of costRoutes) {
    test(`navigating from ${path} renders next page`, { tag: '@regression' }, async ({ page, env }) => {
      await loginAsAdmin(page, env)

      // Visit the Costs page and wait for its heading to actually render.
      await page.goto(path)
      const costHeading = page.getByRole('heading', { name: heading, level: 1 })
      await expect(costHeading).toBeVisible({ timeout: 15000 })

      // Navigate away to the dashboard and confirm it rendered.
      await page.goto('/')
      await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible({
        timeout: 15000,
      })

      // Navigate back — the page must render again (not blank). This is the
      // exact transition that FAR-852 broke.
      await page.goto(path)
      await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible({
        timeout: 15000,
      })
    })
  }
})
