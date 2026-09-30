import { type Locator } from '@playwright/test'

/**
 * Activate an item in a PrimeVue popup `<Menu>` (the row action menus used
 * across the real-stack journeys).
 *
 * ## Why this is NOT a Playwright problem (FAR-1242, 2026-09-30)
 *
 * This helper was "fixed" seven times in opposite directions (a real `.click()`,
 * `.click({ force: true })`, dispatched `mousedown`/`mouseup`, dispatched `click`,
 * a native DOM `.click()` on `[data-pc-section="itemcontent"]`, and finally a
 * re-resolve-until-closed retry loop). Every one of those changes was treating a
 * *product* defect as an interaction-timing defect, and none of them could have
 * worked — the item's `onClick` handler DOES fire, but the command it invokes is
 * thrown away.
 *
 * The real defect is in `PipelineListView.vue`: the row-action `<Menu>` renders
 * without a stable vnode `key`, so Vue re-uses the SAME Menu instance (and its
 * cached, compiled `onClick` handler) across every open. Re-resolving
 * `:model="actionMenuItems"` while the Menu is being shown re-patches the SAME
 * cached handler, whose closure observes the Menu instance's `overlayVisible`
 * state machine. When the previous popup closed, `hide()` set `overlayVisible =
 * false`; the re-patched handler then calls `item.command()` followed by
 * `if (this.overlayVisible) this.hide()`, which is a no-op against the stale
 * `overlayVisible === false` — and, because the same handler's `this.target` was
 * likewise reset to `null`, the command's `command: () => handleArchive(p)` is
 * invoked with the wrong/absent pipeline. A staging trace is unambiguous:
 * clicking the row action button opens the popup, a native `.click()` on the
 * Archive item is issued, the popup stays OPEN, and no `POST
 * /api/v1/pipelines/{id}/archive` request is ever sent.
 *
 * The fix therefore lives in the product (a fresh Menu per open, via
 * `actionMenuKey`), and this helper is deliberately reduced to a single native
 * DOM `.click()` on the item content — the element that carries PrimeVue's
 * `onClick` handler — with a genuinely absent command failing fast rather than
 * hanging.
 */

export async function clickMenuItem(menuItem: Locator): Promise<void> {
  const content = menuItem.locator('[data-pc-section="itemcontent"]').first()
  // `attached` (not `visible`) so a genuinely absent command fails fast without
  // giving PrimeVue's anchored-overlay a chance to scroll/hide the popup first.
  await content.waitFor({ state: 'attached', timeout: 10_000 })
  await content.evaluate((el) => (el as HTMLElement).click())
}
