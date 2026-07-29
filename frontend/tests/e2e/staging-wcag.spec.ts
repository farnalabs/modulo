import { test, expect, loginAsAdmin } from './setup/fixtures'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

const knownViolations = new Set(['color-contrast'])

test.describe('WCAG AA audit (local)', { tag: "@regression" }, () => {
  test('login page has no WCAG AA violations', async ({ page }) => {
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
    expect(results.violations.filter(v => !knownViolations.has(v.id))).toEqual([])
  })

  // Core authenticated pages â€” sampled to avoid full-suite timeout cascades
  const authedPages = [
    '/pipelines', '/stages', '/schemas',
    '/admin/connectors', '/admin/model-backends',
  ]

  for (const pagePath of authedPages) {
    test(`${pagePath} has no WCAG AA violations when authenticated`, async ({ page, env }) => {
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
      expect(results.violations.filter(v => !knownViolations.has(v.id))).toEqual([])
    })
  }
})
