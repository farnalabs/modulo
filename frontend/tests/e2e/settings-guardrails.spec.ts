import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings Guardrails', { tag: "@regression" }, () => {
  test('renders the Guardrails page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/guardrails')
    await expect(page).toHaveURL(/\/settings\/guardrails$/)
    await expect(page.locator('h1')).toContainText('Guardrails')
    if (env.name === 'local') {
      await expect(page.getByTestId('settings-guardrails-create')).toBeVisible()
      await expect(page.getByText('No guardrails configured')).toBeVisible()
    }
  })

  test('opens the create-guardrail dialog with its form fields', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/guardrails')
    await expect(page.locator('h1')).toContainText('Guardrails')
    await page.getByTestId('settings-guardrails-create').click()
    await expect(page.getByTestId('settings-guardrails-form-name')).toBeVisible()
    await expect(page.getByTestId('settings-guardrails-form-pipeline')).toBeVisible()
    await expect(page.getByTestId('settings-guardrails-form-action')).toBeVisible()
    await expect(page.getByTestId('settings-guardrails-form-detection')).toBeVisible()
    await expect(page.getByTestId('settings-guardrails-form-field')).toBeVisible()
    await expect(page.getByTestId('settings-guardrails-form-pattern')).toBeVisible()
    await expect(page.getByTestId('settings-guardrails-form-disclosure')).toBeVisible()
  })
})
