import { type Locator } from '@playwright/test'

/**
 * Activate an item in a PrimeVue popup `<Menu>` (the row action menus used
 * across the real-stack journeys).
 *
 * Clicking a PrimeVue menu item is the single most brittle interaction in the
 * staging suite — it has now been "fixed" six times in opposite directions:
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
 *      rename/archive silently no-op.
 *   5. a real Playwright `.click()` on the item (even after waiting for the
 *      transition): measured 5/6 — it intermittently loses the race with the
 *      anchored-overlay enter transition ("element is not stable" / "element
 *      was detached from the DOM").
 *   6. a NATIVE DOM `.click()` — but only AFTER `expect(menuItem).toBeVisible()`
 *      and a 200 ms settle. The trace for the failing staging run shows the
 *      menu is OPEN when the row action button is clicked and CLOSED by the
 *      time that `toBeVisible` resolves, so the subsequent
 *      `content.evaluate(...)` waits forever (detached element) and the test
 *      burns its 180 s timeout. Any Playwright actionability/wait step gives
 *      PrimeVue a chance to hide the popup first: `Menu.vue` binds a
 *      `ConnectedOverlayScrollHandler` on show that hides the overlay on any
 *      scroll, and `focusMenuList()` (also on show) can scroll the list into
 *      view. The rely-on-a-stable-popup pattern is therefore wrong.
 *
 * The reliable activation is a NATIVE DOM `.click()` on the
 * `[data-pc-section="itemcontent"]` `<div>` — the element that carries
 * PrimeVue's `onClick` handler (`primevue/menu/Menuitem.vue`) — issued
 * IMMEDIATELY after the menu opens, with no visibility check, no settle wait
 * and no intermediate actionability step that could scroll the popup away. A
 * bounded `attached` wait (which does not scroll or require visibility) keeps
 * a genuinely absent command failing fast instead of hanging to the test
 * timeout.
 */
export async function clickMenuItem(menuItem: Locator): Promise<void> {
  const content = menuItem.locator('[data-pc-section="itemcontent"]').first()
  await content.waitFor({ state: 'attached', timeout: 10_000 })
  await content.evaluate((el) => (el as HTMLElement).click())
}
