import { test, expect } from './setup/fixtures'
import AxeBuilder from '@axe-core/playwright'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

const ACCEPTABLE_VIOLATIONS = ['color-contrast', 'scrollable-region-focusable']

function filterViolations(violations: { id: string }[]) {
  return violations.filter(v => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}

test.describe('WCAG AA audit (CI — Vite dev server)', () => {
  const pages = [
    { path: '/login', name: 'login page' },
    { path: '/', name: 'root page' },
  ]

  for (const { path, name } of pages) {
    test(`${name} — light mode has no unexpected WCAG AA violations`, async ({ page }) => {
      await page.goto(path)
      await page.waitForLoadState('networkidle')

      await page.evaluate(() => {
        document.documentElement.classList.add('light')
        document.documentElement.classList.remove('dark')
      })
      await page.waitForTimeout(100)

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (light) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })

    test(`${name} — dark mode has no unexpected WCAG AA violations`, async ({ page }) => {
      await page.goto(path)
      await page.waitForLoadState('networkidle')

      await page.evaluate(() => {
        document.documentElement.classList.remove('light')
        document.documentElement.classList.remove('dark')
      })
      await page.waitForTimeout(100)

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (dark) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })
  }
})
