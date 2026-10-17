import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Copy Pipeline - regression', () => {
  test('navigates to /pipelines/copy and does not redirect to /pipelines', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Requires a pipeline in the database')
    await loginAsAdmin(page, env)
    // Registered AFTER loginAsAdmin: setupLocalMockApi installs a catch-all
    // `**/api/v1/**` route and Playwright matches the LAST-registered route
    // first, so a pipeline stub registered before it is silently shadowed.
    await page.route('**/api/v1/pipelines?page_size=100', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'tp1', name: 'CI/CD Pipeline', description: 'Standard workflow', visibility: 'org', created_at: '2025-06-01T10:00:00Z' }], total: 1, page: 1, page_size: 100 }) })
    })
    await page.goto('/pipelines/copy')
    await expect(page).toHaveURL('/pipelines/copy')
    await expect(page.locator('h1')).toContainText('Copy Pipeline')
  })

  test('shows empty state when no pipelines exist', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Requires a pipeline in the database')
    await loginAsAdmin(page, env)
    // Registered AFTER loginAsAdmin — see the note in the test above; a stub
    // registered before the catch-all never receives the request, which is
    // what made this empty-state assertion fail against the populated list.
    await page.route('**/api/v1/pipelines?page_size=100', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 100 }) })
    })
    await page.goto('/pipelines/copy')
    await expect(page).toHaveURL('/pipelines/copy')
    // `no_pipelines_available` renders as a heading PLUS an actionable hint, so
    // a `.or()` across the two is a strict-mode violation (both match). Assert
    // the heading, then the hint — pinning the no-pipelines-at-all empty state
    // rather than a filter/search-empty variant.
    await expect(page.getByText('No pipelines available', { ignoreCase: true })).toBeVisible()
    await expect(page.getByText('Create one from the Library', { ignoreCase: true })).toBeVisible()
  })
})
