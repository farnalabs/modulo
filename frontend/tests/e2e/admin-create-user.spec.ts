import { test, expect, setupLocalMockApi, loginAsAdmin } from './setup/fixtures'
import type { Page } from '@playwright/test'

async function selectRole(page: Page, value: string) {
  await page.getByTestId('admin-users-create-role').click()
  await page.locator(`[data-value="${value}"]`).click()
}

test.describe('Admin Create User', { tag: ['@regression'] }, () => {
  test('opens create user dialog and submits user creation', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')

    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()

    await page.getByTestId('admin-users-add-user').click()

    await expect(page.getByRole('dialog')).toBeVisible()
    await expect(page.getByText('Create User')).toBeVisible()

    await page.getByTestId('admin-users-create-email').fill('newuser@example.com')
    await page.getByTestId('admin-users-create-display-name').fill('New User')
    await page.getByTestId('admin-users-create-password').fill('Password1')
    await selectRole(page, 'operator')

    await page.getByRole('button', { name: 'Create' }).click()

    // FAR-460: a successful create closes the create dialog and opens the
    // shared credential dialog so the admin can copy the hand-typed password
    // exactly once. Assert THAT contract — the create form is gone and the
    // credential is surfaced — rather than "no dialog is visible", which the
    // shipped behaviour can never satisfy (a correct implementation always
    // leaves the credential dialog on screen).
    await expect(page.getByTestId('admin-users-create-email')).not.toBeVisible()
    await expect(page.getByTestId('admin-users-credential-value')).toBeVisible()
    await expect(page.getByTestId('admin-users-credential-value')).toHaveText('Password1')
  })

  test('shows validation error for invalid email', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')

    await page.getByTestId('admin-users-add-user').click()

    await page.getByTestId('admin-users-create-email').fill('bad-email')
    await page.getByTestId('admin-users-create-display-name').fill('New User')
    await page.getByTestId('admin-users-create-password').fill('Password1')
    await selectRole(page, 'operator')

    await page.getByRole('button', { name: 'Create' }).click()

    await expect(page.getByText('Please enter a valid email address')).toBeVisible()
  })

  test('fills create user form via keyboard Enter', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')

    await page.getByTestId('admin-users-add-user').click()

    await page.getByTestId('admin-users-create-email').fill('enter@example.com')
    await page.getByTestId('admin-users-create-display-name').fill('Enter User')
    await page.getByTestId('admin-users-create-password').fill('Password1')
    await selectRole(page, 'admin')

    await page.getByTestId('admin-users-create-password').press('Enter')

    // Same FAR-460 post-create contract as the click-to-submit test above: the
    // create dialog closes and the credential dialog is surfaced.
    await expect(page.getByTestId('admin-users-create-email')).not.toBeVisible()
    await expect(page.getByTestId('admin-users-credential-value')).toBeVisible()
    await expect(page.getByTestId('admin-users-credential-value')).toHaveText('Password1')
  })
})
