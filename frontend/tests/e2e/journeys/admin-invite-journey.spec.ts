import { test, expect, loginAsAdmin } from '../setup/fixtures'
import { apiBaseFor, apiFetch, apiLogin } from '../setup/realstack-api'

/**
 * Real-stack invite journey (FAR-1242 batch 1).
 *
 * Runs against the REAL backend (staging/app); skips the local mock target.
 * The invite seam creates a real invitation row (persisted) and the revoke
 * removes it — both fully self-cleaning, so nothing is left on the shared
 * instance. Deepens admin-create-user.spec.ts, which only runs on the local
 * mock and stops at "the dialog closed".
 */

interface InvitationItem {
  id: string
  email: string
}

interface InvitationListResponse {
  items: InvitationItem[]
}

test.describe('Real-stack journeys: user invitation lifecycle', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('inviting a user creates a pending invitation that can be revoked', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const email =
      `e2e-invite-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e8).toString(36)}` +
      '@example.com'
    try {
      await loginAsAdmin(page, env)
      await page.goto('/admin/users')

      // Invite mode of the Add User dialog.
      await page.getByTestId('admin-users-add-user').click()
      const dialog = page.getByRole('dialog')
      await expect(dialog).toBeVisible()
      await page.getByTestId('admin-users-mode-invite').click()
      await page.getByTestId('admin-users-create-email').fill(email)
      await page.getByTestId('admin-users-create-display-name').fill('E2E Invite Journey')
      await dialog.getByRole('button', { name: 'Send Invitation' }).click()

      // Observable effect: the one-time invite link is issued...
      await expect(page.getByTestId('admin-users-credential-value')).toBeVisible({ timeout: 30_000 })

      // The invite-link dialog is modal; dismiss it before touching the rest
      // of the page. Its overlay mask covers the invitations table, so the
      // revoke click below is intercepted until the dialog is closed.
      await page.getByTestId('admin-users-invite-done').click()
      await expect(page.getByTestId('admin-users-credential-value')).toBeHidden()

      // ...and the pending invitations section lists the new invitation.
      const invitationRow = page.locator('[data-testid="admin-invitations-row"]').filter({ hasText: email })
      await expect(invitationRow).toBeVisible({ timeout: 30_000 })

      // Persisted: the invitation exists on the backend.
      const listRes = await apiFetch<InvitationListResponse>(apiBase, token, 'GET', '/api/v1/admin/users/invitations?page=1&page_size=100')
      expect(listRes.status).toBe(200)
      expect(listRes.body?.items.some((inv) => inv.email === email)).toBe(true)

      // Revoke through the UI (row-scoped: other invitations may be pending).
      await invitationRow.getByTestId('admin-invitations-revoke').click()
      await page.getByTestId('admin-invitations-confirm-revoke').click()

      // Observable effect: the invitation disappears from the pending list...
      await expect(invitationRow).toHaveCount(0, { timeout: 30_000 })
      // ...and it is really gone from the backend.
      const afterRes = await apiFetch<InvitationListResponse>(apiBase, token, 'GET', '/api/v1/admin/users/invitations?page=1&page_size=100')
      expect(afterRes.status).toBe(200)
      expect(afterRes.body?.items.some((inv) => inv.email === email)).toBe(false)
    } finally {
      // Self-cleaning guarantee: if the UI revoke never landed, drop the
      // invitation directly so the shared instance keeps no residue. This
      // must never throw over the test's own result.
      try {
        const listRes = await apiFetch<InvitationListResponse>(apiBase, token, 'GET', '/api/v1/admin/users/invitations?page=1&page_size=100')
        const leftover = listRes.body?.items?.find((inv) => inv.email === email)
        if (leftover) {
          await apiFetch(apiBase, token, 'DELETE', `/api/v1/admin/users/invitations/${leftover.id}`)
        }
      } catch (err) {
        console.warn('[realstack] cleanup: invitation lookup failed:', err instanceof Error ? err.message : String(err))
      }
    }
  })
})
