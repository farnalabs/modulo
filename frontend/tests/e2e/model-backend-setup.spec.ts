import { test, expect, loginAsAdmin } from './setup/fixtures'

// Structural e2e for /setup/model-backend/:id (FAR-638, batch A). The one-time
// setup token is delivered in the URL FRAGMENT (#token=...), never the query
// string; the view reads it on mount and strips it from the address bar.
// Without a token the page must render a loud warning; with one it renders
// the API-key form and the submit reaches the complete-setup endpoint.
const BACKEND_ID = 'e2e-backend-1'

test.describe('Model Backend Setup', { tag: '@regression' }, () => {
  test('renders the missing-token warning when the fragment token is absent', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto(`/setup/model-backend/${BACKEND_ID}`)

    await expect(page).toHaveURL(new RegExp(`/setup/model-backend/${BACKEND_ID}`))
    await expect(page.locator('h1')).toContainText('Complete Model Backend Setup')
    await expect(page.getByText('missing its one-time token')).toBeVisible()
  })

  test('renders the API-key form and completes setup with a fragment token', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/model-backends/${BACKEND_ID}/complete-setup`, (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', backend_id: BACKEND_ID, name: 'E2E Backend' }),
      })
    })

    await page.goto(`/setup/model-backend/${BACKEND_ID}#token=e2e-setup-token`)

    await expect(page.locator('h1')).toContainText('Complete Model Backend Setup')
    const apiKeyInput = page.locator('input[type="password"]')
    await expect(apiKeyInput).toBeVisible()

    await apiKeyInput.fill('sk-e2e-test-key')
    await page.getByRole('button', { name: 'Complete Setup' }).click()

    await expect(page.getByText('is now active.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'View Model Backends' })).toBeVisible()
  })
})
