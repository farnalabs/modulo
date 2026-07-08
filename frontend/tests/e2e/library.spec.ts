import { test, expect } from './setup/fixtures'

test.describe('Library Page', () => {
  test('displays page title', async ({ page }) => {
    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Library')
  })

  test('shows type filter dropdown', async ({ page }) => {
    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    const filter = page.getByTestId('library-type-filter')
    await expect(filter).toBeVisible()

    const options = await filter.locator('option').allTextContents()
    expect(options.length).toBeGreaterThan(1)
  })

  test('shows Create Pipeline button in header', async ({ page }) => {
    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    const createBtn = page.getByTestId('library-create-pipeline-header')
    await expect(createBtn).toBeVisible()
    await expect(createBtn).toContainText('Create Pipeline')
  })
})
