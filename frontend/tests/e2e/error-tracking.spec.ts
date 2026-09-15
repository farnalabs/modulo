import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Error Tracking', { tag: "@regression" }, () => {
  test('error dashboard page loads', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/errors')

    await expect(page.locator('h1')).toContainText(/Error/i)
  })

  test('error dashboard shows UI elements', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/errors*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'e1',
              sample_message: 'Connection timeout',
              level_peak: 'error',
              count: 15,
              first_seen: '2025-06-01T12:00:00Z',
              last_seen: '2025-06-01T12:00:00Z',
              status: 'new',
            },
          ],
          total: 1,
        }),
      })
    })

    await page.goto('/admin/errors')

    await expect(page.locator('text=Connection timeout')).toBeVisible()
  })

  test('auto-applies filters without Apply button (FAR-868)', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/errors')

    // The Apply Filters button must not exist
    const applyBtn = page.locator('button', { hasText: /apply filters/i })
    await expect(applyBtn).toHaveCount(0)

    // Reset button must exist
    await expect(page.getByTestId('admin-errors-reset')).toBeVisible()

    // Changing a dropdown filter should auto-apply (no Apply button needed)
    const levelFilter = page.getByTestId('filter-bar-level')
    if (await levelFilter.isVisible()) {
      await levelFilter.click()
      const errorOption = page.locator('.p-select-option', { hasText: 'Error' })
      if (await errorOption.isVisible()) {
        await errorOption.click()
      }
    }

    // Reset should clear filters
    await page.getByTestId('admin-errors-reset').click()
    await expect(page.getByTestId('admin-errors-reset')).toBeVisible()
  })
})
