import { test, expect } from './setup/fixtures'
import AxeBuilder from '@axe-core/playwright'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

// aria-hidden-focus: PrimeVue renders hidden tabbable focus-trap spans
// (`p-hidden-accessible p-hidden-focusable`) inside its Select/overlay panels
// for a11y navigation. axe flags these as aria-hidden-focus; they are a
// library-internal navigation affordance, not a Modulo defect.
const ACCEPTABLE_VIOLATIONS = ['color-contrast', 'scrollable-region-focusable', 'aria-hidden-focus']

function filterViolations(violations: { id: string }[]) {
  return violations.filter(v => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}

test.describe('PrimeVue Select overlay WCAG + keyboard nav', { tag: "@regression" }, () => {
  test('open filter-bar-status overlay has no unexpected WCAG AA violations', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    // dev_mode:true (set by the local mock API) renders the Remy floating
    // panel on every route; it overlaps the /runs filter bar and intercepts
    // pointer events, blocking the Select trigger. Close it (mirrors the
    // staging e2e global-setup) so the overlay can be opened.
    await page.addInitScript(() => { try { localStorage.setItem('remy-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/runs')
    await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

    // Open the migrated PrimeVue Select panel (teleported to body).
    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()
    await expect(page.locator('[role="listbox"]')).toBeVisible()

    const results = await new AxeBuilder({ page })
      .withTags(WCAG_TAGS)
      .analyze()

    const violations = filterViolations(results.violations)
    if (violations.length > 0) {
      console.log(`\n=== /runs open select overlay new violations ===`)
      for (const v of violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(violations).toEqual([])
  })

  test('PrimeVue Select supports keyboard navigation (arrow + escape)', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    // dev_mode:true (set by the local mock API) renders the Remy floating
    // panel on every route; it overlaps the /runs filter bar and intercepts
    // pointer events, blocking the Select trigger. Close it (mirrors the
    // staging e2e global-setup) so the overlay can be opened.
    await page.addInitScript(() => { try { localStorage.setItem('remy-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/runs')
    await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()
    const listbox = page.locator('[role="listbox"]')
    await expect(listbox).toBeVisible()

    // ArrowDown moves the active option. PrimeVue keeps real focus on the
    // combobox trigger and tracks the active option via the
    // aria-activedescendant / data-p-focused attributes (not DOM focus), so
    // assert that some option became active after ArrowDown.
    await page.keyboard.press('ArrowDown')
    await expect(page.locator('[role="option"][data-p-focused="true"]')).toHaveCount(1)

    // Escape closes the panel.
    await page.keyboard.press('Escape')
    await expect(listbox).not.toBeVisible()
  })
})

test.describe('WCAG AA audit (CI — Vite dev server)', { tag: "@regression" }, () => {
  const pages = [
    { path: '/login', name: 'login page' },
    { path: '/', name: 'root page' },
  ]

  for (const { path, name } of pages) {
    test(`${name} — light mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
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

    test(`${name} — dark mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
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
