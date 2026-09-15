import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('FilterBar Select overlay positioning', { tag: '@regression' }, () => {
  test('Select dropdown overlay anchors beneath its trigger, not at the viewport left edge', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    // Mock the notifications API so the page renders quickly
    await page.route('**/api/v1/notifications/in-app*', (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.includes('/dashboard')) { route.fallback(); return }
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }),
      })
    })

    await page.goto('/notifications')

    // Wait for the FilterBar select to be visible
    const select = page.getByRole('combobox', { name: 'Level' })
    await expect(select).toBeVisible()

    // Open the dropdown
    await select.click()

    // The overlay (listbox) should be visible
    const overlay = page.getByRole('listbox')
    await expect(overlay).toBeVisible()

    // Capture bounding boxes
    const selectBox = await select.boundingBox()
    const overlayBox = await overlay.boundingBox()
    expect(selectBox).not.toBeNull()
    expect(overlayBox).not.toBeNull()

    // The overlay's left edge must be within 50px of the trigger's left edge
    // (the old bug placed it at the viewport left edge, i.e. left ≈ 0)
    const leftOffset = Math.abs(overlayBox!.x - selectBox!.x)
    expect(leftOffset).toBeLessThan(50)

    // The overlay must NOT be at the viewport's left edge (left < 10px is the
    // body-appended mispositioning symptom)
    expect(overlayBox!.x).toBeGreaterThan(10)

    // The overlay's top must be below the trigger's bottom (anchored beneath)
    const triggerBottom = selectBox!.y + selectBox!.height
    expect(overlayBox!.y).toBeGreaterThanOrEqual(triggerBottom - 5) // allow 5px tolerance
  })
})
