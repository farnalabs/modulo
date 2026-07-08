import { test, expect } from './setup/fixtures'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

test.describe('WCAG AA audit (staging.modulo.run)', () => {
  test('login page has no WCAG AA violations', async ({ page }) => {
    test.setTimeout(30000)
    await page.goto('https://staging.modulo.run/login')
    await page.waitForLoadState('networkidle')

    const AxeBuilder = (await import('@axe-core/playwright')).default
    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

    if (results.violations.length > 0) {
      console.log('\n=== /login (staging) violations ===')
      for (const v of results.violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(results.violations).toEqual([])
  })

  test('dashboard has no WCAG AA violations when authenticated', async ({ page }) => {
    test.setTimeout(30000)
    await page.goto('https://staging.modulo.run/login')
    await page.waitForLoadState('networkidle')

    // Log in with staging credentials
    const inputs = await page.locator('input').all()
    if (inputs.length >= 2) {
      await inputs[0].fill('admin@demo.modulo')
      await inputs[1].fill('admin123')
    }
    await page.locator('button[type="submit"]').click()
    await page.waitForURL(url => !url.includes('/login'), { timeout: 10000 }).catch(() => {})

    const AxeBuilder = (await import('@axe-core/playwright')).default
    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

    if (results.violations.length > 0) {
      console.log('\n=== / (staging, authed) violations ===')
      for (const v of results.violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(results.violations).toEqual([])
  })

  // Core authenticated pages
  const authedPages = [
    '/pipelines', '/stages', '/evals', '/schemas',
    '/settings/license', '/settings/teams',
    '/admin/users', '/admin/connectors',
    '/admin/model-backends', '/admin/cost-overview',
  ]

  for (const pagePath of authedPages) {
    test(`${pagePath} has no WCAG AA violations when authenticated`, async ({ page }) => {
      test.setTimeout(30000)
      await page.goto('https://staging.modulo.run/login')
      await page.waitForLoadState('networkidle')

      const inputs = await page.locator('input').all()
      if (inputs.length >= 2) {
        await inputs[0].fill('admin@demo.modulo')
        await inputs[1].fill('admin123')
      }
      await page.locator('button[type="submit"]').click()
      await page.waitForURL(url => !url.includes('/login'), { timeout: 10000 }).catch(() => {})

      await page.goto('https://staging.modulo.run' + pagePath)
      await page.waitForLoadState('networkidle')

      const AxeBuilder = (await import('@axe-core/playwright')).default
      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()

      if (results.violations.length > 0) {
        console.log(`\n=== ${pagePath} (staging, authed) violations ===`)
        for (const v of results.violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }
      expect(results.violations).toEqual([])
    })
  }
})
