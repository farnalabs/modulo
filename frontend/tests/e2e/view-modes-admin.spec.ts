import { test, expect } from '@playwright/test'

const sampleViews = {
  items: [
    {
      id: 'v1',
      name: 'Active Runs',
      view_type: 'table',
      filters: { status: 'active' },
      columns: ['name', 'status', 'created_at'],
      sort_by: 'created_at',
      sort_order: 'desc',
      created_by: 'alice@test.com',
      created_at: '2025-01-15T10:00:00Z',
    },
    {
      id: 'v2',
      name: 'Kanban Board',
      view_type: 'kanban',
      filters: null,
      columns: ['title', 'assignee'],
      sort_by: 'priority',
      sort_order: 'asc',
      created_by: 'bob@test.com',
      created_at: '2025-02-20T14:30:00Z',
    },
  ],
}

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('View Modes Admin CRUD', () => {
  test('page loads and shows header + Create View button', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Saved Views')
    await expect(page.getByTestId('admin-views-add')).toBeVisible()
    await expect(page.getByTestId('admin-views-add')).toContainText('Create View')
  })

  test('shows loading state while fetching views', async ({ page }) => {
    let resolvePromise: () => void
    const neverResolve = new Promise<void>((resolve) => { resolvePromise = resolve })
    const originalRoute = page.route('**/api/v1/views', async (route) => {
      await neverResolve
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/admin/views')

    const spinner = page.locator('.animate-spin')
    await expect(spinner).toBeVisible({ timeout: 3000 })
    resolvePromise!()
    await originalRoute
  })

  test('shows error with retry button on API failure', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Server error' }) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Failed to load views')).toBeVisible({ timeout: 5000 })
    await expect(page.locator('button:has-text("Retry")')).toBeVisible()
  })

  test('shows empty state when no views exist', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=No saved views yet')).toBeVisible()
    await expect(page.locator('text=Learn about saved views')).toBeVisible()
  })

  test('create a new view with all fields', async ({ page }) => {
    let createdPayload: unknown = null
    await page.route('**/api/v1/views', async (route, request) => {
      if (request.method() === 'GET') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
      } else if (request.method() === 'POST') {
        createdPayload = request.postDataJSON()
        await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({}) })
      } else {
        await route.fulfill({ status: 405, body: '' })
      }
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await page.getByTestId('admin-views-add').click()
    await expect(page.locator('text=New View')).toBeVisible()

    await page.getByTestId('admin-views-name-input').fill('My Test View')
    await page.getByTestId('admin-views-type-select').selectOption('grid')
    await page.getByTestId('admin-views-filters-input').fill('{"status": "active", "env": "prod"}')
    await page.getByTestId('admin-views-columns-input').fill('name, status, created_at')
    await page.getByTestId('admin-views-sort-by-input').fill('created_at')
    await page.getByTestId('admin-views-sort-order-select').selectOption('asc')

    await page.getByTestId('admin-views-save').click()
    await page.waitForLoadState('networkidle')

    expect(createdPayload).toEqual({
      name: 'My Test View',
      view_type: 'grid',
      filters: { status: 'active', env: 'prod' },
      columns: ['name', 'status', 'created_at'],
      sort_by: 'created_at',
      sort_order: 'asc',
    })
  })

  test('shows validation error when name is empty', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await page.getByTestId('admin-views-add').click()
    await page.getByTestId('admin-views-name-input').fill('')
    await page.getByTestId('admin-views-save').click()

    const validationMessage = await page.getByTestId('admin-views-name-input').evaluate((el: HTMLInputElement) => el.validationMessage)
    expect(validationMessage).toBeTruthy()
  })

  test('cancel create form clears fields', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await page.getByTestId('admin-views-add').click()
    await expect(page.locator('text=New View')).toBeVisible()

    await page.getByTestId('admin-views-name-input').fill('Temporary View')
    await page.getByTestId('admin-views-cancel').click()

    await expect(page.locator('text=New View')).not.toBeVisible({ timeout: 3000 })
    await expect(page.getByTestId('admin-views-add')).toBeVisible()
  })

  test('edit an existing view shows pre-populated fields', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    const editButtons = page.getByTestId('admin-views-edit')
    await expect(editButtons).toHaveCount(2)
    await editButtons.first().click()

    await expect(page.locator('text=Edit View')).toBeVisible()
    await expect(page.getByTestId('admin-views-name-input')).toHaveValue('Active Runs')
    await expect(page.getByTestId('admin-views-type-select')).toHaveValue('table')
    await expect(page.getByTestId('admin-views-filters-input')).toContainText('status')
    await expect(page.getByTestId('admin-views-columns-input')).toHaveValue('name, status, created_at')
    await expect(page.getByTestId('admin-views-sort-by-input')).toHaveValue('created_at')
    await expect(page.getByTestId('admin-views-sort-order-select')).toHaveValue('desc')
  })

  test('delete a view with confirmation', async ({ page }) => {
    await page.route('**/api/v1/views', async (route, request) => {
      if (request.method() === 'GET') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
      } else if (request.method() === 'DELETE') {
        await route.fulfill({ status: 204, body: '' })
      } else {
        await route.fulfill({ status: 405, body: '' })
      }
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    const deleteButtons = page.getByTestId('admin-views-delete')
    await expect(deleteButtons).toHaveCount(2)
    await deleteButtons.first().click()

    await expect(page.locator('text=Delete "Active Runs"?')).toBeVisible()
    await expect(page.getByTestId('admin-views-delete-confirm')).toBeVisible()
    await expect(page.getByTestId('admin-views-delete-cancel')).toBeVisible()

    await page.getByTestId('admin-views-delete-cancel').click()
    await expect(page.locator('text=Delete "Active Runs"?')).not.toBeVisible({ timeout: 3000 })

    await deleteButtons.first().click()
    await page.getByTestId('admin-views-delete-confirm').click()
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Delete "Active Runs"?')).not.toBeVisible({ timeout: 3000 })
  })

  test('displays existing views in table', async ({ page }) => {
    await page.route('**/api/v1/views', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
    })
    await login(page)

    await page.goto('/admin/views')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Active Runs')).toBeVisible()
    await expect(page.locator('text=Kanban Board')).toBeVisible()
    await expect(page.locator('text=alice@test.com')).toBeVisible()
    await expect(page.locator('text=bob@test.com')).toBeVisible()

    const tableRows = page.locator('table tbody tr')
    await expect(tableRows).toHaveCount(2)
  })
})
