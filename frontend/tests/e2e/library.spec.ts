import { test, expect, loginAsAdmin } from './setup/fixtures'
import { getTestEnv } from './setup/env'

test.describe('Library Page', () => {
  test('displays page title', async ({ page }) => {
    await page.route('**/api/v1/library*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'l1', name: 'Code Review Agent', type: 'agent', description: 'Reviews pull requests', created_at: '2025-06-01T10:00:00Z' }, { id: 'l2', name: 'Deploy Workflow', type: 'workflow', description: 'CI/CD pipeline template', created_at: '2025-06-02T10:00:00Z' }], total: 2 }) })
    })
    await loginAsAdmin(page, getTestEnv())

    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Library')
  })

  test('shows type filter dropdown', async ({ page }) => {
    await page.route('**/api/v1/library*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'l1', name: 'Code Review Agent', type: 'agent', description: 'Reviews pull requests', created_at: '2025-06-01T10:00:00Z' }, { id: 'l2', name: 'Deploy Workflow', type: 'workflow', description: 'CI/CD pipeline template', created_at: '2025-06-02T10:00:00Z' }], total: 2 }) })
    })
    await loginAsAdmin(page, getTestEnv())

    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    const filter = page.getByTestId('library-type-filter')
    await expect(filter).toBeVisible()

    const options = await filter.locator('option').allTextContents()
    expect(options.length).toBeGreaterThan(1)
  })

  test('shows Create Pipeline button in header', async ({ page }) => {
    await page.route('**/api/v1/library*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'l1', name: 'Code Review Agent', type: 'agent', description: 'Reviews pull requests', created_at: '2025-06-01T10:00:00Z' }, { id: 'l2', name: 'Deploy Workflow', type: 'workflow', description: 'CI/CD pipeline template', created_at: '2025-06-02T10:00:00Z' }], total: 2 }) })
    })
    await loginAsAdmin(page, getTestEnv())

    await page.goto('/library')
    await page.waitForLoadState('networkidle')

    const createBtn = page.getByTestId('library-create-pipeline-header')
    await expect(createBtn).toBeVisible()
    await expect(createBtn).toContainText('Create Pipeline')
  })
})
