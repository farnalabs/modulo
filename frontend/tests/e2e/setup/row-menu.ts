import { expect, type Locator, type Page } from '@playwright/test'

/**
 * Activate an item in a PrimeVue popup `<Menu>` (the row action menus used
 * across the real-stack journeys).
 *
 * Clicking a PrimeVue menu item is the single most brittle interaction in the
 * staging suite — it has now been "fixed" three times in opposite directions:
 *
 *   1. a real `.click()` on the inner `<a class="p-menu-item-link">` — never
 *      completes: the anchored overlay's enter transition
 *      (`p-anchored-overlay`) keeps the element moving across frames, so
 *      Playwright's actionability wait loops on "element is not stable";
 *   2. `.click({ force: true })` — skips the stability gate but computes the
 *      click point before the overlay settles and dispatches at stale
 *      coordinates, so the first interaction after the menu opens no-ops;
 *   3. `dispatchEvent('click')` — a synthetic event with `isTrusted === false`,
 *      which Ripple (bound to `mousedown`, not click) does not prevent. The
 *      click target is the anchored `<a href="/pipelines">` shipped in every
 *      row action item's command payload, so the browser navigates to
 *      `/pipelines` and tears down the SPA before the rename/archive dialog
 *      can open.
 *
 * Dispatch a REAL pointer click (`mousedown` + `mouseup`) on the inner
 * `[data-pc-section="itemcontent"]` `<div>` — the element that carries
 * PrimeVue's `@click` handler (`primevue/menu/Menuitem.vue`). `mouseup` fires a synthetic
 * `click`; because it originates from a real `mousedown`, Ripple calls
 * `preventDefault()` on it, so the anchor's navigation is suppressed while the
 * click still bubbles into PrimeVue's handler. Being part of a real
 * pointer sequence, consumers cannot distinguish it from a hardware click,
 * and it needs no geometry — so an in-flight overlay transition cannot move
 * the target out from under it.
 *
 * The item is asserted visible first, so a genuinely missing command still
 * fails.
 */
export async function clickMenuItem(page: Page, menuItem: Locator): Promise<void> {
  await expect(menuItem).toBeVisible({ timeout: 15_000 })
  const content = menuItem.locator('[data-pc-section="itemcontent"]').first()
  await content.dispatchEvent('mousedown')
  await content.dispatchEvent('mouseup')
}
