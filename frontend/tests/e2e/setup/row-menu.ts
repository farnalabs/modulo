import { expect, type Locator, type Page } from '@playwright/test'

/**
 * Activate an item in a PrimeVue popup `<Menu>` (the row action menus used
 * across the real-stack journeys).
 *
 * Clicking a PrimeVue menu item is the single most brittle interaction in the
 * staging suite — it has now been "fixed" four times in opposite directions:
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
 *      synthesise the browser's input pipeline, so a dispatched `mouseup`
 *      never produces a `click`. PrimeVue binds the command to `onClick`
 *      (`primevue/menu/Menuitem.vue`), so the handler never ran at all and
 *      every rename/archive/delete journey failed with "dialog not found".
 *      (Verified in Chromium: mousedown+mouseup on the content div produces
 *      zero `click` events.)
 *
 * Dispatch a `click` directly on the `[data-pc-section="itemcontent"]` `<div>`
 * — the element that carries PrimeVue's `onClick` handler. A dispatched click
 * fires that handler regardless of geometry or an in-flight overlay
 * transition, which is exactly why the coordinate-based approaches above
 * failed. The item is asserted visible first, so a genuinely missing command
 * still fails.
 */
export async function clickMenuItem(page: Page, menuItem: Locator): Promise<void> {
  await expect(menuItem).toBeVisible({ timeout: 15_000 })
  const content = menuItem.locator('[data-pc-section="itemcontent"]').first()
  await content.dispatchEvent('click')
}
