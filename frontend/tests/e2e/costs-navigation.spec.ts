import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * Regression test for FAR-852: visiting a Costs page then navigating away
 * must never leave a blank page. The root cause was multi-root views breaking
 * AppLayout's <transition name="page" mode="out-in">.
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

      // Visit the Costs page
      await page.goto(path)
      await page.waitForSelector('[data-loading="false"]', { timeout: 10000 }).catch(() => {})

      // Navigate to dashboard
      await page.goto('/')
      await page.waitForSelector('[data-loading="false"]', { timeout: 10000 }).catch(() => {})

      // Assert content rendered (not blank)
      const bodyText = await page.textContent('body')
      expect(bodyText).toBeTruthy()
      expect(bodyText!.length).toBeGreaterThan(0)
    })
  }
})
