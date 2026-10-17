import { test, expect, loginAsAdmin } from './setup/fixtures'
import { WCAG_TAGS, filterViolations } from './setup/wcag'

test.describe('WCAG AA audit (local)', { tag: "@regression" }, () => {
  test('login page has no WCAG AA violations', { tag: "@regression" }, async ({ page }) => {
    test.setTimeout(30000)
    await page.goto('/login')

    const AxeBuilder = (await import('@axe-core/playwright')).default
    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

    if (results.violations.length > 0) {
      console.log('\n=== /login violations ===')
      for (const v of results.violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(filterViolations(results.violations)).toEqual([])
  })

  // Core authenticated pages — sampled to avoid full-suite timeout cascades
  const authedPages = [
    '/pipelines', '/schemas',
    '/admin/connectors', '/admin/model-backends',
  ]

  for (const pagePath of authedPages) {
    test(`${pagePath} has no WCAG AA violations when authenticated`, { tag: "@regression" }, async ({ page, env }) => {
      test.setTimeout(30000)
      await loginAsAdmin(page, env)

      await page.goto(pagePath)

      const AxeBuilder = (await import('@axe-core/playwright')).default
      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

      if (results.violations.length > 0) {
        console.log(`\n=== ${pagePath} violations ===`)
        for (const v of results.violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }
      expect(filterViolations(results.violations)).toEqual([])
    })
  }
})
