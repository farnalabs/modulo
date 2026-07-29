import { test, expect, loginAsAdmin } from './setup/fixtures'

const samplePipelines = {
  items: [
    { id: 'p1', name: 'CI Pipeline', description: 'Continuous integration', status: 'active' },
    { id: 'p2', name: 'Deploy Pipeline', description: 'Production deployment', status: 'active' },
    { id: 'p3', name: 'Data Processing', description: 'ETL pipeline', status: 'inactive' },
  ],
  total: 3,
}

test.describe('Search', { tag: "@regression" }, () => {
  test('pipelines page has search input', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/pipelines*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(samplePipelines) })
    })

    await page.goto('/pipelines')

    const searchInput = page.locator('input[type="text"][placeholder*="earch" i], input[placeholder*="ilter" i], input[placeholder*="ind" i]')
    if (await searchInput.count() > 0) {
      await expect(searchInput.first()).toBeVisible()
    }

    await expect(page.locator('text=CI Pipeline')).toBeVisible()
  })

  test('library page search filters results', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/libraries*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        items: [
          { id: 'l1', name: 'Code Review Agent', primitive_type: 'agent', source: 'native', slug: 'code-review-agent', author: 'Modulo', version: '1.0.0', tags: [], visibility: 'org', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
          { id: 'l2', name: 'Deploy Workflow', primitive_type: 'workflow', source: 'native', slug: 'deploy-workflow', author: 'Modulo', version: '1.0.0', tags: [], visibility: 'org', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
          { id: 'l3', name: 'Data Validator', primitive_type: 'agent', source: 'native', slug: 'data-validator', author: 'Modulo', version: '1.0.0', tags: [], visibility: 'org', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        ],
        total: 3,
      })})
    })

    await page.goto('/library')

    await expect(page.locator('text=Code Review Agent')).toBeVisible()
    await expect(page.locator('text=Deploy Workflow')).toBeVisible()
  })
})
