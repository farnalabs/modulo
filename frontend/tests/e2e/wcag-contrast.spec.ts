import { test, expect } from './setup/fixtures'
import { type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

const ACCEPTABLE_VIOLATIONS = ['color-contrast', 'scrollable-region-focusable']

const CONTEXT_DESTROYED = /Execution context was destroyed|Target page, context or browser has been closed/i

function filterViolations(violations: { id: string }[]) {
  return violations.filter(v => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}

async function runAxeAudit(page: Page) {
  // The SPA can navigate shortly after load (router guard redirects, Vite
  // lazy-compile full reloads), which destroys the execution context while
  // axe runs its in-page analysis. Wait for the page to settle first, and
  // retry if a navigation still tears the context down mid-analyze.
  await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {})
  await page.waitForTimeout(250)

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      return await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()
    } catch (err) {
      if (attempt === 3 || !(err instanceof Error && CONTEXT_DESTROYED.test(err.message))) {
        throw err
      }
      await page.waitForLoadState('domcontentloaded', { timeout: 15_000 }).catch(() => {})
      await page.waitForTimeout(250)
    }
  }

  throw new Error('axe analysis failed after retries')
}

test.describe('WCAG AA audit (CI â€” Vite dev server)', { tag: "@regression" }, () => {
  const pages = [
    { path: '/login', name: 'login page' },
    { path: '/', name: 'root page' },
  ]

  for (const { path, name } of pages) {
    test(`${name} â€” light mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
      await page.goto(path)
      await page.waitForURL('**/*', { timeout: 5000 }).catch(() => {})

      // Guard: storageState may redirect /login to / (dashboard)
      if (path === '/login' && !page.url().includes('/login')) {
        console.log(`  Skipping ${path} — redirected by valid session`)
        return
      }

      // Wait for the Vue app to mount before running axe
      await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

      await page.evaluate(() => {
        document.documentElement.classList.add('light')
        document.documentElement.classList.remove('dark')
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      const results = await runAxeAudit(page)

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (light) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })

    test(`${name} â€” dark mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
      await page.goto(path)
      await page.waitForURL('**/*', { timeout: 5000 }).catch(() => {})

      // Guard: storageState may redirect /login to / (dashboard)
      if (path === '/login' && !page.url().includes('/login')) {
        console.log(`  Skipping ${path} — redirected by valid session`)
        return
      }

      // Wait for the Vue app to mount before running axe
      await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

      await page.evaluate(() => {
        document.documentElement.classList.remove('light')
        document.documentElement.classList.remove('dark')
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      const results = await runAxeAudit(page)

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
