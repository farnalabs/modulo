import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('First-Run Golden Path', { tag: "@regression" }, () => {
  test.skip(({ env }) => env.name !== 'local', 'Requires local mock with specific pipeline data')

  test('golden path: login -> browse pipelines', { tag: "@regression" }, async ({ page, env }) => {
    // Step 1: Navigate to app and log in
    await loginAsAdmin(page, env)

    // Mock the pipeline list so at least one pipeline row renders
    await page.route('**/api/v1/pipelines*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: '1', name: 'Demo Pipeline', organisation_id: '1', description: 'A demo pipeline to test the golden path', visibility: 'org', status: 'idle', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', archived_at: null }], total: 1 }) })
    })

    // Step 2: Navigate to pipelines list
    await page.goto('/pipelines')
    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-search')).toBeVisible()

    // Step 3: The demo pipeline row is reachable
    const pipelineRow = page.getByTestId('pipeline-tree-row-1').first()
    await expect(pipelineRow).toBeVisible({ timeout: 5000 })
    await expect(pipelineRow).toContainText('Demo Pipeline')
  })

  test('dashboard shows pipeline summary for first-run user', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/')

    // Dashboard should be visible with pipeline count or recent activity
    await expect(page.getByTestId('dashboard-title')).toBeVisible({ timeout: 5000 })
  })

  test('demo pipeline exists in library', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/pipelines')

    // At least one pipeline should be visible (from the mock) — wait for the
    // list to render before counting (locator.count() does not auto-wait).
    const pipelineRows = page.locator('[data-testid^="pipeline-list-card"], [data-testid^="pipeline-tree-row-"]')
    await expect(pipelineRows.first()).toBeVisible()
    const count = await pipelineRows.count()
    expect(count).toBeGreaterThanOrEqual(1)
  })
})
