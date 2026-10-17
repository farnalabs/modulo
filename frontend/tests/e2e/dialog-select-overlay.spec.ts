import { test, expect, setupLocalMockApi, loginForSystemRoute } from './setup/fixtures'
import type { Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { WCAG_TAGS, filterViolations } from './setup/wcag'

/**
 * FAR-1908 — a Select rendered inside a dialog must not have its overlay
 * clipped by the dialog's scrolling content box.
 *
 * `.p-dialog-content` is a scroll container (`overflow-y: auto`). With
 * `AppSelect`'s `appendTo: 'self'` default the overlay is rendered inside that
 * container, so options that fall below the content box are painted outside the
 * visible area and `.p-dialog-footer` covers them: the option is "visible,
 * enabled and stable" to Playwright yet a real click at the point where the
 * option is drawn lands on the footer and selects nothing (FAR-1898's
 * "div.p-dialog-footer intercepts pointer events").
 *
 * Playwright's own `click()` hides the bug because it auto-scrolls the option
 * into view first, which is why the defect only surfaced intermittently under
 * load (the slower the render, the more likely the option is still below the
 * fold when the click is computed). These tests therefore assert the invariant
 * directly — the point where an option is drawn must hit that option — and
 * perform a raw `mouse.click` at that point so no auto-scroll rescues it.
 *
 * The overlay is portalled to `<body>`, OUTSIDE the dialog's `aria-modal`
 * subtree, so the keyboard and axe cases below also prove that moving the
 * overlay out of the dialog's scroll box did not cost keyboard reachability or
 * accessibility.
 */

/** Open the create-user dialog without opening any Select overlay. */
async function openCreateUserDialog(page: Page) {
  await page.goto('/admin/users')
  await page.getByTestId('admin-users-add-user').click()
  await expect(page.getByRole('dialog')).toBeVisible()
}

/** Open the dialog and its role Select overlay via a pointer click. */
async function openRoleSelect(page: Page) {
  await openCreateUserDialog(page)
  await page.getByTestId('admin-users-create-role').click()
  await expect(page.getByRole('listbox')).toBeVisible()
}

test.describe('Dialog Select overlay', () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name !== 'local', 'local mock only')
    await setupLocalMockApi(page)
    await loginForSystemRoute(page, env)
  })

  test('renders its overlay outside the dialog scroll container @regression', async ({ page }) => {
    await openRoleSelect(page)

    const overlayIsInsideContent = await page.evaluate(() => {
      const overlay = document.querySelector('.p-select-overlay')
      return !!(overlay && overlay.closest('.p-dialog-content'))
    })

    expect(overlayIsInsideContent).toBe(false)
  })

  test('every option is the topmost element at its own centre @regression', async ({ page }) => {
    await openRoleSelect(page)

    const probe = await page.evaluate(() => {
      const options = Array.from(document.querySelectorAll('.p-select-overlay .p-select-option'))
      const misses = options.flatMap((el) => {
        const r = el.getBoundingClientRect()
        const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
        if (el === top || (top != null && el.contains(top))) return []
        return [{ text: (el.textContent ?? '').trim(), hit: top?.className ?? 'null' }]
      })
      return { count: options.length, misses }
    })

    // Guard against a vacuous pass: the selector must actually match the
    // four role options before "none of them are covered" means anything.
    expect(probe.count).toBe(4)
    expect(probe.misses).toHaveLength(0)
  })

  test('clicking an option where it is drawn selects it @regression', async ({ page }) => {
    await openRoleSelect(page)

    const centre = await page
      .locator('[data-value="operator"]')
      .first()
      .evaluate((el) => {
        const r = el.getBoundingClientRect()
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 }
      })

    // Raw pointer click: exactly what a user aiming at the drawn option does.
    await page.mouse.click(centre.x, centre.y)

    await expect(page.getByTestId('admin-users-create-role').locator('.p-select-label')).toHaveText(
      'Operator',
    )
  })

  test('options stay keyboard-selectable inside the modal @regression', async ({ page }) => {
    await openCreateUserDialog(page)

    // Drive the Select entirely from its combobox with the keyboard — the real
    // user path, and the only way to prove the dialog's focus trap tolerates an
    // overlay that lives outside its `aria-modal` subtree. (Force-focusing the
    // listbox, as this test used to, skips the trap and so proves nothing about
    // it.)
    const trigger = page.getByTestId('admin-users-create-role').getByRole('combobox')
    await trigger.focus()
    await expect(trigger).toBeFocused()

    // Enter on a closed Select opens the overlay and activates the first option.
    await trigger.press('Enter')
    await expect(page.getByRole('listbox')).toBeVisible()
    // Focus must remain on the trigger; PrimeVue tracks the active option via
    // `data-p-focused` / `aria-activedescendant` rather than moving DOM focus.
    await expect(trigger).toBeFocused()

    // Walk to the Admin option, asserting the tracker followed, then commit.
    // Asserting the ACTIVE OPTION (not an option index) is what makes this a
    // keyboard-reachability test rather than index arithmetic.
    await trigger.press('ArrowDown')
    await trigger.press('ArrowDown')
    await expect(page.locator('[role="option"][data-p-focused="true"]')).toHaveAttribute(
      'aria-label',
      'Admin',
    )

    await trigger.press('Enter')

    await expect(page.getByTestId('admin-users-create-role').locator('.p-select-label')).toHaveText(
      'Admin',
    )
    // Selecting must not have dismissed the modal.
    await expect(page.getByRole('dialog')).toBeVisible()
  })

  test('open overlay inside the dialog has no unexpected WCAG AA violations @regression', async ({
    page,
  }) => {
    await openRoleSelect(page)

    // Scope the audit to the dialog and its portalled overlay: that pair is the
    // surface FAR-1908 moved, and unscoped page noise would mask a regression in
    // exactly the elements under test.
    const results = await new AxeBuilder({ page })
      .include('.p-dialog')
      .include('.p-select-overlay')
      .withTags(WCAG_TAGS)
      .analyze()

    // Non-vacuity: `openRoleSelect` has already asserted the dialog and listbox
    // are visible, and axe must have evaluated at least one rule inside the
    // scoped contexts for an empty violation list to mean anything.
    expect(results.passes.length).toBeGreaterThan(0)

    const violations = filterViolations(results.violations)
    if (violations.length > 0) {
      console.log(`\n=== /admin/users open dialog select overlay new violations ===`)
      for (const v of violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(violations).toEqual([])
  })
})
