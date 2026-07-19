import { test, expect, loginAsAdmin } from './setup/fixtures'

const sampleBackends = {
  items: [
    {
      id: 'b1',
      name: 'gpt-4',
      display_name: 'GPT-4',
      provider: 'openai',
      model_id: 'gpt-4',
      has_credentials: true,
      tier: 'native',
      visibility: 'org',
      created_by: 'admin@test.com',
      created_at: '2025-01-15T10:00:00Z',
    },
  ],
}

test.describe('Admin Model Backends', () => {
  test('page loads with correct heading and add button', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/model-backends*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleBackends) })
    })

    await page.goto('/admin/model-backends')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Model Backends')
    await expect(page.getByTestId('admin-model-backends-add')).toBeVisible()
  })

  test('shows existing backends in the list', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/model-backends*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sampleBackends) })
    })

    await page.goto('/admin/model-backends')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=GPT-4').first()).toBeVisible()
    await expect(page.getByTestId('admin-model-backends-edit').first()).toBeVisible()
    await expect(page.getByTestId('admin-model-backends-delete').first()).toBeVisible()
  })
})
