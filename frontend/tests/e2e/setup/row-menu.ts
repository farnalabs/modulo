import { expect, type Locator, type Page } from '@playwright/test'

/**
 * Activate an item in a PrimeVue popup `<Menu>` (the row action menus used
 * across the real-stack journeys).
 *
 * Clicking a PrimeVue menu item is the single most brittle interaction in the
 * staging suite — it has now been "fixed" five times in opposite directions:
 *
 *   1. a real `.click()` on the inner `<a class="p-menu-item-link">` — never
 *      completes: the anchored overlay's enter transition moves the element
 *      across frames, so Playwright's actionability wait loops on "element is
 *      not stable";
 *   2. `.click({ force: true })` — skips the stability gate but computes the
 *      click point before the overlay settles and dispatches at stale
 *      coordinates, so the first interaction after the menu opens no-ops;
 *   3. `dispatchEvent('mousedown')` + `dispatchEvent('mouseup')` — WRONG.
 *      `dispatchEvent` dispatches exactly the event it is given; it does NOT
 *      synthesise the browser's input pipeline, so neither a dispatched
 *      `mouseup` produces a `click` nor does the handler ever run.
 *   4. `dispatchEvent('click')` on `[data-pc-section="itemcontent"]` — also
 *      WRONG. Re-verified against the deployed staging build (Chromium 149):
 *      the element resolves, but PrimeVue's `onClick` never fires, so
 *      rename/archive silently no-op. The pipeline-lifecycle journeys failed
 *      with "dialog not found" / the row never leaving the list while no
 *      archive request was ever sent.
 *   5. a real Playwright `.click()` on the item (even after waiting for the
 *      transition): measured 5/6 — it intermittently loses the race with the
 *      anchored-overlay enter transition ("element is not stable" / "element
 *      was detached from the DOM").
 *
 * The reliable activation is a NATIVE DOM `.click()` on the
 * `[data-pc-section="itemcontent"]` `<div>` — the element that carries
 * PrimeVue's `onClick` handler (`primevue/menu/Menuitem.vue`). Measured 6/6
 * against the deployed build: it runs the handler regardless of geometry or an
 * in-flight overlay transition, while still failing if the command is
 * genuinely absent (the item is asserted visible first).
 */
export async function clickMenuItem(page: Page, menuItem: Locator): Promise<void> {
  await expect(menuItem).toBeVisible({ timeout: 15_000 })
  // Let the anchored-overlay enter transition settle so the popup DOM is
  // stable before we invoke the handler.
  await page.waitForTimeout(200)
  const content = menuItem.locator('[data-pc-section="itemcontent"]').first()
  await content.evaluate((el) => (el as HTMLElement).click())
}
