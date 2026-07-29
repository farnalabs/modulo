import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Copy Pipeline - regression', () => {
  test('navigates to /pipelines/copy and does not redirect to /pipelines', { tag: '@regression' }, async ({ page, env }) => {
    await page.route('**/api/v1/pipelines?page_size=100', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'tp1', name: 'CI/CD Pipeline', description: 'Standard workflow', visibility: 'org', created_at: '2025-06-01T10:00:00Z' }], total: 1, page: 1, page_size: 100 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/pipelines/copy')
    await expect(page).toHaveURL('/pipelines/copy')
    await expect(page.locator('h1')).toContainText('Copy Pipeline')
  })

  test('shows empty state when no pipelines exist', { tag: '@regression' }, async ({ page, env }) => {
    await page.route('**/api/v1/pipelines?page_size=100', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 100 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/pipelines/copy')
    await expect(page).toHaveURL('/pipelines/copy')
    await expect(page.getByText('no pipelines available', { ignoreCase: true }).or(page.getByText('create one', { ignoreCase: true }))).toBeVisible()
  })
})
