import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Stages Board', () => {
  test('stages board page loads', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/stages')

    await expect(page.locator('h1')).toContainText(/Stage|Board/i)
  })

  test('stages board shows columns and elements', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/stages*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { id: 's1', name: 'Development', runs_count: 3 },
            { id: 's2', name: 'Review', runs_count: 5 },
            { id: 's3', name: 'Production', runs_count: 1 },
          ],
          total: 3,
        }),
      })
    })

    await page.goto('/stages')

    await expect(page.getByTestId('stage-board-column-s1')).toBeVisible()
    await expect(page.getByTestId('stage-board-column-s2')).toBeVisible()
    await expect(page.getByTestId('stage-board-column-s3')).toBeVisible()
  })
})
