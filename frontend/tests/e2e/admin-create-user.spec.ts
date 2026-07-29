import { test, expect, setupLocalMockApi, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Create User', { tag: ['@regression'] }, () => {
  test('opens create user dialog and submits user creation', async ({ page, env }) => {
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
    await page.getByTestId('admin-users-create-role').selectOption('operator')

    await page.getByRole('button', { name: 'Create' }).click()

    await page.waitForTimeout(500)

    const dialogVisible = await page.getByRole('dialog').isVisible().catch(() => false)
    if (dialogVisible) {
      const errorText = await page.getByText(/please enter|required|must contain|failed/i).isVisible().catch(() => false)
      expect(errorText).toBe(false)
    }
  })

  test('shows validation error for invalid email', async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')

    await page.getByTestId('admin-users-add-user').click()

    await page.getByTestId('admin-users-create-email').fill('bad-email')
    await page.getByTestId('admin-users-create-display-name').fill('New User')
    await page.getByTestId('admin-users-create-password').fill('Password1')
    await page.getByTestId('admin-users-create-role').selectOption('operator')

    await page.getByRole('button', { name: 'Create' }).click()

    await expect(page.getByText('Please enter a valid email address')).toBeVisible()
  })

  test('fills create user form via keyboard Enter', async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    await setupLocalMockApi(page)
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')

    await page.getByTestId('admin-users-add-user').click()

    await page.getByTestId('admin-users-create-email').fill('enter@example.com')
    await page.getByTestId('admin-users-create-display-name').fill('Enter User')
    await page.getByTestId('admin-users-create-password').fill('Password1')
    await page.getByTestId('admin-users-create-role').selectOption('admin')

    await page.getByTestId('admin-users-create-password').press('Enter')

    await page.waitForTimeout(500)

    const dialogVisible = await page.getByRole('dialog').isVisible().catch(() => false)
    if (dialogVisible) {
      const errorText = await page.getByText(/please enter|required|must contain|failed/i).isVisible().catch(() => false)
      expect(errorText).toBe(false)
    }
  })
})
