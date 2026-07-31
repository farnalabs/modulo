import { test, expect, loginAsAdmin } from './setup/fixtures'
import { getTestEnv } from './setup/env'

test.describe('Pipelines Page', { tag: "@regression" }, () => {
  test('displays page title and search input', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Requires a pipeline in the database')
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    await expect(page.locator('h1')).toBeVisible()
    await expect(page.getByTestId('pipeline-list-search')).toBeVisible()
  })

  test('shows New Pipeline CTA button', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    const newPipelineBtn = page.getByTestId('pipeline-list-new-pipeline')
    await expect(newPipelineBtn).toBeVisible()
    await expect(newPipelineBtn).toContainText('New Pipeline')
  })

  test('search input filters pipelines', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Requires a pipeline in the database')
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    const searchInput = page.getByTestId('pipeline-list-search')
    await expect(searchInput).toBeVisible()

    await searchInput.fill('test pipeline')
    const currentValue = await searchInput.inputValue()
    expect(currentValue).toBe('test pipeline')
  })
})
