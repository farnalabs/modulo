import { test, expect, loginAsAdmin } from './setup/fixtures'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

const knownViolations = new Set(['color-contrast', 'aria-valid-attr-value'])

test.describe('WCAG AA audit (staging.modulo.run)', () => {
  test('login page has no WCAG AA violations', async ({ page }) => {
    test.setTimeout(30000)
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    const AxeBuilder = (await import('@axe-core/playwright')).default
    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

    if (results.violations.length > 0) {
      console.log('\n=== /login (staging) violations ===')
      for (const v of results.violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(results.violations.filter(v => !knownViolations.has(v.id))).toEqual([])
  })

  test('dashboard has no WCAG AA violations when authenticated', async ({ page, env }) => {
    test.setTimeout(30000)
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await loginAsAdmin(page, env)

    const AxeBuilder = (await import('@axe-core/playwright')).default
    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

    if (results.violations.length > 0) {
      console.log('\n=== / (staging, authed) violations ===')
      for (const v of results.violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(results.violations.filter(v => !knownViolations.has(v.id))).toEqual([])
  })

  // Core authenticated pages
  const authedPages = [
    '/pipelines', '/stages', '/evals', '/schemas',
    '/settings/license', '/settings/teams',
    '/admin/users', '/admin/connectors',
    '/admin/model-backends',     '/admin/costs',
  ]

  for (const pagePath of authedPages) {
    test(`${pagePath} has no WCAG AA violations when authenticated`, async ({ page, env }) => {
      test.setTimeout(30000)
      await page.goto('/login')
      await page.waitForLoadState('networkidle')

      await loginAsAdmin(page, env)

      await page.goto(pagePath)
      await page.waitForLoadState('networkidle')

      const AxeBuilder = (await import('@axe-core/playwright')).default
      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

      if (results.violations.length > 0) {
        console.log(`\n=== ${pagePath} (staging, authed) violations ===`)
        for (const v of results.violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }
      expect(results.violations.filter(v => !knownViolations.has(v.id))).toEqual([])
    })
  }
})
