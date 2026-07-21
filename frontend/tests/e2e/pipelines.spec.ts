import { test, expect, loginAsAdmin } from './setup/fixtures'
import { getTestEnv } from './setup/env'

test.describe('Pipelines Page', { tag: '@staging-regression' }, () => {
  test('displays page title and search input', async ({ page }) => {
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-search')).toBeVisible()
  })

  test('shows New Pipeline CTA button', async ({ page }) => {
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    const newPipelineBtn = page.getByTestId('pipeline-list-new-pipeline')
    await expect(newPipelineBtn).toBeVisible()
    await expect(newPipelineBtn).toContainText('New Pipeline')
  })

  test('search input filters pipelines', async ({ page }) => {
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    const searchInput = page.getByTestId('pipeline-list-search')
    await expect(searchInput).toBeVisible()

    await searchInput.fill('test pipeline')
    const currentValue = await searchInput.inputValue()
    expect(currentValue).toBe('test pipeline')
  })
})
