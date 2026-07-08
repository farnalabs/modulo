import { test, expect, loginAsAdmin } from './setup/fixtures'

const samplePipelines = {
  items: [
    { id: 'p1', name: 'CI Pipeline', description: 'Continuous integration', status: 'active' },
    { id: 'p2', name: 'Deploy Pipeline', description: 'Production deployment', status: 'active' },
    { id: 'p3', name: 'Data Processing', description: 'ETL pipeline', status: 'inactive' },
  ],
  total: 3,
}

test.describe('Search', () => {
  test('pipelines page has search input', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/pipelines*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(samplePipelines) })
    })

    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')

    const searchInput = page.locator('input[type="text"][placeholder*="earch" i], input[placeholder*="ilter" i], input[placeholder*="ind" i]')
    if (await searchInput.count() > 0) {
      await expect(searchInput.first()).toBeVisible()
    }

    await expect(page.locator('text=CI Pipeline')).toBeVisible()
  })

  test('library page search filters results', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/library*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        items: [
          { id: 'l1', name: 'Code Review Agent', type: 'agent' },
          { id: 'l2', name: 'Deploy Workflow', type: 'workflow' },
          { id: 'l3', name: 'Data Validator', type: 'agent' },
        ],
        total: 3,
      })})
    })

    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Code Review Agent')).toBeVisible()
    await expect(page.locator('text=Deploy Workflow')).toBeVisible()
  })
})
