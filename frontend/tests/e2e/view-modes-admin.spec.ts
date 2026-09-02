import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'
import type { Page } from '@playwright/test'

async function selectOption(page: Page, testId: string, value: string) {
  await page.getByTestId(testId).click()
  await page.locator(`[data-value="${value}"]`).click()
}

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

test.describe('View Modes Admin CRUD', { tag: "@regression" }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    // The Remy floating panel (rendered on staging where dev-mode is on) opens
    // by default and its fixed-position overlay intercepts clicks on the
    // header's Create View button, timing out locator.click() with
    // "subtree intercepts pointer events". Force the panel closed before the
    // app boots so it can never cover page controls. The small Remy FAB (shown
    // only when the panel is closed) sits bottom-right and never overlaps.
    await page.addInitScript(() => {
      localStorage.setItem('remy-panel-state', 'closed')
    })
  })

  test('page loads and shows header + Create View button', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await expect(page.locator('h1')).toContainText('Saved Views')
    await expect(page.getByTestId('admin-views-add')).toBeVisible()
    await expect(page.getByTestId('admin-views-add')).toContainText('Create View')
  })

  test('shows loading state while fetching views', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', async (route) => {
      await new Promise(r => setTimeout(r, 3000))
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    const spinner = page.locator('.animate-spin')
    await expect(spinner).toBeVisible({ timeout: 5000 })
  })

  test('shows error with retry button on API failure', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Server error' }) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await expect(page.getByText('Server error')).toBeVisible({ timeout: 5000 })
    await expect(page.getByTestId('admin-views-error')).toBeVisible({ timeout: 5000 })
  })

  test('shows empty state when no views exist', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await expect(page.locator('text=No saved views yet')).toBeVisible()
    await expect(page.locator('text=Learn about saved views')).toBeVisible()
  })

  test('create a new view with all fields', { tag: "@regression" }, async ({ page, env }) => {
    let createdPayload: unknown = null
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', async (route, request) => {
      if (request.method() === 'GET') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
      } else if (request.method() === 'POST') {
        createdPayload = request.postDataJSON()
        await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({}) })
      } else {
        await route.fulfill({ status: 405, body: '' })
      }
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await page.getByTestId('admin-views-add').click()
    await expect(page.locator('text=New View')).toBeVisible()

    // Guard the form fields explicitly — an intercepted/slow interaction (e.g.
    // a floating overlay capturing the first click) previously timed out here.
    const nameInput = page.getByTestId('admin-views-name-input')
    await expect(nameInput).toBeVisible()
    await expect(nameInput).toBeEnabled()
    await nameInput.fill('My Test View')
    const typeSelect = page.getByTestId('admin-views-type-select')
    await expect(typeSelect).toBeVisible()
    await selectOption(page, 'admin-views-type-select', 'grid')
    await page.getByTestId('admin-views-filters-input').fill('{"status": "active", "env": "prod"}')
    await page.getByTestId('admin-views-columns-input').fill('name, status, created_at')
    await page.getByTestId('admin-views-sort-by-input').fill('created_at')
    const sortOrderSelect = page.getByTestId('admin-views-sort-order-select')
    await expect(sortOrderSelect).toBeVisible()
    await selectOption(page, 'admin-views-sort-order-select', 'asc')

    const saveButton = page.getByTestId('admin-views-save')
    await expect(saveButton).toBeVisible()
    await page.getByTestId('admin-views-save').click()

    expect(createdPayload).toEqual({
      name: 'My Test View',
      view_type: 'grid',
      filters: { status: 'active', env: 'prod' },
      columns: ['name', 'status', 'created_at'],
      sort_by: 'created_at',
      sort_order: 'asc',
    })
  })

  test('shows validation error when name is empty', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await page.getByTestId('admin-views-add').click()
    const emptyNameInput = page.getByTestId('admin-views-name-input')
    await expect(emptyNameInput).toBeVisible()
    await expect(emptyNameInput).toBeEnabled()
    await page.getByTestId('admin-views-name-input').fill('')
    const emptyNameSave = page.getByTestId('admin-views-save')
    await expect(emptyNameSave).toBeVisible()
    await page.getByTestId('admin-views-save').click()

    const validationMessage = await page.getByTestId('admin-views-name-input').evaluate((el: HTMLInputElement) => el.validationMessage)
    expect(validationMessage).toBeTruthy()
  })

  test('cancel create form clears fields', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await page.getByTestId('admin-views-add').click()
    await expect(page.locator('text=New View')).toBeVisible()

    const cancelNameInput = page.getByTestId('admin-views-name-input')
    await expect(cancelNameInput).toBeVisible()
    await expect(cancelNameInput).toBeEnabled()
    await page.getByTestId('admin-views-name-input').fill('Temporary View')
    const cancelButton = page.getByTestId('admin-views-cancel')
    await expect(cancelButton).toBeVisible()
    await page.getByTestId('admin-views-cancel').click()

    await expect(page.locator('text=New View')).not.toBeVisible({ timeout: 3000 })
    await expect(page.getByTestId('admin-views-add')).toBeVisible()
  })

  test('edit an existing view shows pre-populated fields', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    const editButtons = page.locator('table tbody').getByRole('button', { name: 'Edit' })
    await expect(editButtons).toHaveCount(2)
    await editButtons.first().evaluate((el: HTMLElement) => el.click())

    await expect(page.getByTestId('admin-views-form-title')).toHaveText('Edit View')
    await expect(page.getByTestId('admin-views-name-input')).toHaveValue('Active Runs')
    await expect(page.getByTestId('admin-views-type-select')).toContainText('Table')
    await expect(page.getByTestId('admin-views-filters-input')).toHaveValue(/status/)
    await expect(page.getByTestId('admin-views-columns-input')).toHaveValue('name, status, created_at')
    await expect(page.getByTestId('admin-views-sort-by-input')).toHaveValue('created_at')
    await expect(page.getByTestId('admin-views-sort-order-select')).toContainText('Descending')
  })

  test('delete a view with confirmation', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', async (route, request) => {
      if (request.method() === 'GET') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
      } else if (request.method() === 'DELETE') {
        await route.fulfill({ status: 204, body: '' })
      } else {
        await route.fulfill({ status: 405, body: '' })
      }
    })

    await page.goto('/admin/views', { timeout: 120000 })

    const deleteButtons = page.locator('table tbody').getByRole('button', { name: 'Delete' })
    await expect(deleteButtons).toHaveCount(2)
    await deleteButtons.first().evaluate((el: HTMLElement) => el.click())

    await expect(page.getByTestId('admin-views-delete-confirm')).toBeVisible()
    await expect(page.getByTestId('admin-views-delete-cancel')).toBeVisible()
    await expect(page.locator('table tbody').getByRole('button', { name: 'Delete' })).toHaveCount(2)

    await page.getByTestId('admin-views-delete-cancel').evaluate((el: HTMLElement) => el.click())
    await expect(page.getByTestId('admin-views-delete-confirm')).not.toBeVisible({ timeout: 3000 })

    await deleteButtons.first().evaluate((el: HTMLElement) => el.click())
    await page.getByTestId('admin-views-delete-confirm').evaluate((el: HTMLElement) => el.click())

    await expect(page.getByTestId('admin-views-delete-confirm')).not.toBeVisible({ timeout: 3000 })
  })

  test('displays existing views in table', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/views**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleViews) })
    })

    await page.goto('/admin/views', { timeout: 120000 })

    await expect(page.locator('text=Active Runs')).toBeVisible()
    await expect(page.locator('text=Kanban Board')).toBeVisible()
    await expect(page.locator('text=alice@test.com')).toBeVisible()
    await expect(page.locator('text=bob@test.com')).toBeVisible()

    const tableRows = page.locator('table tbody tr')
    await expect(tableRows).toHaveCount(2)
  })
})
