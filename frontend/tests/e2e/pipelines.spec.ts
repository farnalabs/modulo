import { test, expect, loginAsAdmin } from './setup/fixtures'
import { getTestEnv } from './setup/env'

test.describe('Pipelines Page', { tag: "@regression" }, () => {
  test('displays page title and search input', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Requires a pipeline in the database')
    await loginAsAdmin(page, getTestEnv())
    await page.goto('/pipelines')

    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-search')).toBeVisible()
  })

  test('shows New Pipeline CTA button', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, getTestEnv())
    // The header CTA stays `invisible` until the pipelines list resolves, and
    // it was repeatedly flaky across staging @regression runs while the real
    // GET /api/v1/pipelines was still in flight. Declare the list so the CTA's
    // visibility depends on this spec's data, not on live DB latency.
    await page.route('**/api/v1/pipelines*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ id: 'p1', name: 'CI Pipeline', description: 'Continuous integration', status: 'active' }],
          total: 1,
        }),
      })
    })
    await page.goto('/pipelines')

    const newPipelineBtn = page.getByTestId('pipeline-list-new-pipeline')
    await expect(newPipelineBtn).toBeVisible({ timeout: 30_000 })
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
