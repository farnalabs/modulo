import { test, expect, setupLocalMockApi, loginAsAdmin } from './setup/fixtures'

// Proves the PrimeVue Select contract that the Remy `select()` executor relies
// on: clicking a combobox trigger opens a teleported panel whose options carry
// `data-value` (rendered by the `#option` slot), and clicking one selects it.
test.describe('Remy select() contract on migrated Select', { tag: "@regression" }, () => {
  test('filter-bar-status select opens and selects an option via data-value', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/runs')

    const trigger = page.getByTestId('filter-bar-status')
    await expect(trigger).toBeVisible()

    // Remy select() opens the combobox then clicks [data-value="..."].
    await trigger.click()
    const runningOption = page.locator('[data-value="running"]')
    await expect(runningOption).toBeVisible()
    await runningOption.click()

    // The selected value is reflected in the trigger.
    await expect(trigger).toContainText('Running')
  })

  test('migrated Select option renders with a data-value for Remy to target', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/runs')

    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()

    // The #option slot must emit data-value so the Remy executor can target it.
    const failedOption = page.locator('[data-value="failed"]')
    await expect(failedOption).toBeVisible()
    await failedOption.click()
    await expect(trigger).toContainText('Failed')
  })
})
