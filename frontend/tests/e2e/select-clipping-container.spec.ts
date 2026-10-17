import { test, expect, loginAsAdmin } from './setup/fixtures'
import type { Page } from '@playwright/test'

/**
 * FAR-1920 — a Select inside ANY clipping ancestor (not just a PrimeVue
 * dialog) must portal its overlay out of that ancestor.
 *
 * FAR-1908 fixed the class-specific `.p-dialog-content` case. The defect class
 * is wider: any ancestor whose computed overflow is not `visible` clips an
 * in-place overlay, so its lower options are hidden or covered and a click
 * where an option is drawn lands on the wrong element. The layout's scroll
 * region (`<main class="overflow-auto">`) is such an ancestor and hosts the
 * page-level FilterBar Selects, so it is a real, non-dialog reproduction site:
 * with the derived target still `'self'` the overlay renders INSIDE `<main>`;
 * with the general clipping-ancestor predicate it is portalled to `<body>`.
 *
 * The assertion is the general invariant, not a container class — the same
 * shape FAR-1908's dialog spec uses ("overlay is outside the clipping box"),
 * applied to a container that is not a dialog.
 */

/** True when the open overlay sits inside an ancestor that clips its overflow. */
const OVERLAY_INSIDE_CLIPPING_ANCESTOR = () => {
  const CLIPPING = new Set(['hidden', 'clip', 'auto', 'scroll'])
  const overlay = document.querySelector('.p-select-overlay')
  if (!overlay) return { found: false, clipped: false, container: '' }
  let node: Element | null = overlay.parentElement
  while (node && node !== document.body && node !== document.documentElement) {
    const style = getComputedStyle(node)
    const clips =
      CLIPPING.has(style.overflowX) ||
      CLIPPING.has(style.overflowY) ||
      (style.overflow || '')
        .split(/\s+/)
        .some((value) => CLIPPING.has(value))
    if (clips) {
      return {
        found: true,
        clipped: true,
        container: `${node.tagName.toLowerCase()}.${typeof node.className === 'string' ? node.className.split(/\s+/).slice(0, 2).join('.') : ''}`,
      }
    }
    node = node.parentElement
  }
  return { found: true, clipped: false, container: '' }
}

async function openLevelSelect(page: Page) {
  const select = page.getByRole('combobox', { name: 'Level' })
  await expect(select).toBeVisible()
  await select.click()
  await expect(page.getByRole('listbox')).toBeVisible()
}

test.describe('Select overlay escapes any clipping ancestor (FAR-1920)', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name !== 'local', 'local mock only')
    await loginAsAdmin(page, env)
    // Keep /notifications rendering fast and deterministic.
    await page.route('**/api/v1/notifications/in-app*', (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.includes('/dashboard')) {
        route.fallback()
        return
      }
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }),
      })
    })
    await page.goto('/notifications')
  })

  test('renders its overlay outside the layout\'s clipping scroll region', { tag: '@regression' }, async ({
    page,
  }) => {
    await openLevelSelect(page)

    // Non-vacuity: the reproduction is only meaningful if the Select really is
    // hosted inside a clipping ancestor before the overlay is considered.
    const hostIsClipped = await page.evaluate(() => {
      const host = document.querySelector('[data-testid="filter-bar-level"]')
      const CLIPPING = new Set(['hidden', 'clip', 'auto', 'scroll'])
      let node: Element | null = host?.parentElement ?? null
      while (node && node !== document.body && node !== document.documentElement) {
        const style = getComputedStyle(node)
        if (
          CLIPPING.has(style.overflowX) ||
          CLIPPING.has(style.overflowY) ||
          (style.overflow || '')
            .split(/\s+/)
            .some((value) => CLIPPING.has(value))
        ) {
          return true
        }
        node = node.parentElement
      }
      return false
    })
    expect(hostIsClipped).toBe(true)

    const result = await page.evaluate(OVERLAY_INSIDE_CLIPPING_ANCESTOR)
    expect(result.found).toBe(true)
    expect(result.clipped, `overlay is inside clipping ancestor ${result.container}`).toBe(false)
  })

  test('the overlay is portalled to body, not kept under the Select', { tag: '@regression' }, async ({
    page,
  }) => {
    await openLevelSelect(page)
    const keptUnderSelect = await page.evaluate(() => {
      const overlay = document.querySelector('.p-select-overlay')
      const select = document.querySelector('[data-testid="filter-bar-level"]')
      return overlay != null && select != null && select.contains(overlay)
    })
    expect(keptUnderSelect).toBe(false)
  })

  test('still anchors beneath its trigger (FAR-851 not regressed)', { tag: '@regression' }, async ({
    page,
  }) => {
    const select = page.getByRole('combobox', { name: 'Level' })
    await expect(select).toBeVisible()
    const selectBox = await select.boundingBox()
    await select.click()
    const overlay = page.getByRole('listbox')
    await expect(overlay).toBeVisible()
    const overlayBox = await overlay.boundingBox()
    expect(selectBox).not.toBeNull()
    expect(overlayBox).not.toBeNull()
    expect(Math.abs(overlayBox!.x - selectBox!.x)).toBeLessThan(50)
    expect(overlayBox!.x).toBeGreaterThan(10)
    const triggerBottom = selectBox!.y + selectBox!.height
    expect(overlayBox!.y).toBeGreaterThanOrEqual(triggerBottom - 5)
  })
})
