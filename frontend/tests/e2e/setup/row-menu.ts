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
 *
 *   7. the single native DOM `.click()` above is STILL not enough. A staging
 *      trace (2026-09-30, `archiving a pipeline` retry) shows the popup OPEN
 *      immediately before the click and STILL OPEN after it, with no
 *      `/pipelines/{id}/archive` request ever sent: the item list re-rendered
 *      during the anchored-overlay enter transition, so the resolved node was
 *      already detached when `.click()` ran and the native click was a silent
 *      no-op. `Menu.itemClick` hides the popup immediately after invoking the
 *      item's `command`, so a popup that stays open is proof the command did
 *      NOT fire. Re-resolve the item and re-click until the popup closes.
 */

// The anchored-overlay enter/leave animation is 300ms, so the overlay stays
// attached for ~300ms after hide(); the close budget must exceed that or the
// retry loop would click the departing popup a second time.
const POPUP_CLOSE_POLL_MS = 40
const POPUP_CLOSE_BUDGET_MS = 800
const MAX_CLICK_ATTEMPTS = 10

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export async function clickMenuItem(menuItem: Locator): Promise<void> {
  const content = menuItem.locator('[data-pc-section="itemcontent"]').first()
  await content.waitFor({ state: 'attached', timeout: 10_000 })

  let clicked = false
  for (let attempt = 0; attempt < MAX_CLICK_ATTEMPTS && (await menuItem.count()) > 0; attempt++) {
    try {
      // A Locator re-resolves on each call, so this targets the LIVE node even
      // if the previous attempt lost its handle to a re-render.
      await content.evaluate((el) => (el as HTMLElement).click())
      clicked = true
    } catch (err) {
      // Element detached between resolution and click — retry with a fresh one.
      console.warn('clickMenuItem: menu item click attempt failed, retrying', err)
    }

    // Give Menu.itemClick's hide() a moment to unmount the overlay before
    // deciding whether another attempt is needed (avoids a double command).
    const deadline = Date.now() + POPUP_CLOSE_BUDGET_MS
    while ((await menuItem.count()) > 0 && Date.now() < deadline) {
      await sleep(POPUP_CLOSE_POLL_MS)
    }
  }

  if (!clicked || (await menuItem.count()) > 0) {
    throw new Error('clickMenuItem: row-action popup never closed — the menu command did not fire')
  }
}
