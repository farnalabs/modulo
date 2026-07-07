import { test, expect } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

// WCAG AA tags that include color-contrast rules
const WCAG_AA_TAGS = ['wcag2aa', 'wcag21aa', 'wcag22aa']

test.describe('WCAG color contrast', () => {
  const pages = [
    { path: '/login', name: 'login page' },
    { path: '/', name: 'root page' },
  ]

  for (const { path, name } of pages) {
    test(`${name} — light mode has no AA contrast violations`, async ({ page }) => {
      await page.goto(path)
      await page.waitForLoadState('networkidle')

      // Force light mode
      await page.evaluate(() => {
        document.documentElement.classList.add('light')
        document.documentElement.classList.remove('dark')
      })

      // Small delay for CSS transition
      await page.waitForTimeout(100)

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_AA_TAGS)
        .analyze()

      const contrastViolations = results.violations.filter(
        v => v.id === 'color-contrast' || v.id === 'color-contrast-enhanced',
      )

      expect(contrastViolations).toEqual([])
    })

    test(`${name} — dark mode has no AA contrast violations`, async ({ page }) => {
      await page.goto(path)
      await page.waitForLoadState('networkidle')

      // Force dark mode (default: remove both classes so :root values apply)
      await page.evaluate(() => {
        document.documentElement.classList.remove('light')
        document.documentElement.classList.remove('dark')
      })

      await page.waitForTimeout(100)

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_AA_TAGS)
        .analyze()

      const contrastViolations = results.violations.filter(
        v => v.id === 'color-contrast' || v.id === 'color-contrast-enhanced',
      )

      expect(contrastViolations).toEqual([])
    })
  }
})
