import { test, expect, setupLocalMockApi, loginForSystemRoute } from './setup/fixtures'
import type { Page } from '@playwright/test'

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
 */
async function openCreateUserDialog(page: Page) {
  await page.goto('/admin/users')
  await page.getByTestId('admin-users-add-user').click()
  await expect(page.getByRole('dialog')).toBeVisible()
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
    await openCreateUserDialog(page)

    const overlayIsInsideContent = await page.evaluate(() => {
      const overlay = document.querySelector('.p-select-overlay')
      return !!(overlay && overlay.closest('.p-dialog-content'))
    })

    expect(overlayIsInsideContent).toBe(false)
  })

  test('every option is the topmost element at its own centre @regression', async ({ page }) => {
    await openCreateUserDialog(page)

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
    await openCreateUserDialog(page)

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

    // The overlay is portalled outside the modal's aria-modal subtree, so prove
    // the dialog's focus trap does not make it unreachable by keyboard.
    const listbox = page.getByRole('listbox')
    await listbox.press('ArrowDown')
    await listbox.press('ArrowDown')
    await listbox.press('Enter')

    await expect(page.getByTestId('admin-users-create-role').locator('.p-select-label')).toHaveText(
      'Admin',
    )
    // Selecting must not have dismissed the modal.
    await expect(page.getByRole('dialog')).toBeVisible()
  })
})
