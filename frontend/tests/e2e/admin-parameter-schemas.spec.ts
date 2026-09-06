import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Parameter Schemas', { tag: "@regression" }, () => {
  test('renders the Parameter Schemas page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/parameter-schemas')
    await expect(page).toHaveURL(/\/admin\/parameter-schemas$/)
    await expect(page.locator('h1')).toContainText('Parameter Schemas')
    await expect(page.getByTestId('paramschema-new')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByText('No parameter schemas yet')).toBeVisible()
    }
  })

  test('opens the schema editor from New Schema and returns via Back', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/parameter-schemas')
    await expect(page.locator('h1')).toContainText('Parameter Schemas')
    await page.getByTestId('paramschema-new').click()
    await expect(page.getByTestId('paramschema-name-input')).toBeVisible()
    await expect(page.getByTestId('paramschema-desc-input')).toBeVisible()
    await expect(page.getByTestId('paramschema-back')).toBeVisible()
    await page.getByTestId('paramschema-back').click()
    await expect(page.locator('h1')).toContainText('Parameter Schemas')
    await expect(page.getByTestId('paramschema-new')).toBeVisible()
  })
})
