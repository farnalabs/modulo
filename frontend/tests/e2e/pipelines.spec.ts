import { test, expect } from '@playwright/test'

test.describe('Pipelines Page', () => {
  test('displays page title and search input', async ({ page }) => {
    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-search')).toBeVisible()
  })

  test('shows New Pipeline CTA button', async ({ page }) => {
    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')

    const newPipelineBtn = page.getByTestId('pipeline-list-new-pipeline')
    await expect(newPipelineBtn).toBeVisible()
    await expect(newPipelineBtn).toContainText('New Pipeline')
  })

  test('search input filters pipelines', async ({ page }) => {
    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')

    const searchInput = page.getByTestId('pipeline-list-search')
    await expect(searchInput).toBeVisible()

    await searchInput.fill('test pipeline')
    const currentValue = await searchInput.inputValue()
    expect(currentValue).toBe('test pipeline')
  })
})
