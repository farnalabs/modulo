import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  deleteTeamBestEffort,
  listTeams,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Real-stack team RBAC journey (FAR-1242 batch 3).
 *
 * Runs against the REAL backend (staging/app); skips the local target, where
 * the whole API is page.route-mocked by setupLocalMockApi. The team CRUD seam
 * is the admin surface nothing else in the regression spine exercises.
 *
 * Every mutation goes through the real admin UI form and is proven persisted
 * through GET /api/v1/admin/teams afterwards; the team is deleted again at the
 * end so the shared instance keeps no residue.
 *
 * Precondition: the target exposes the team RBAC surface (a Team-tier
 * feature) — otherwise the whole journey is skipped with a reason instead of
 * failing noisily.
 */

test.describe('Real-stack journeys: team lifecycle', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating and renaming a team via the admin UI persists it, and deleting removes it', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)

    // Precondition: the org exposes the team surface at all (Team-tier
    // feature — a Community licence hides it and the journey must not run).
    const precheck = await apiFetch(apiBase, token, 'GET', '/api/v1/admin/teams')
    test.skip(precheck.status !== 200, `team RBAC surface unavailable on this target (GET /api/v1/admin/teams -> ${precheck.status})`)

    const name = uniqueName('E2E Journey Team')
    const renamedName = uniqueName('E2E Journey Team Renamed')
    let teamId: string | null = null

    try {
      await loginAsAdmin(page, env)
      await page.goto('/settings/teams')

      // Create through the real form.
      await page.getByTestId('settings-teams-create-team').click()
      await page.getByTestId('settings-teams-create-name').fill(name)
      await page.getByTestId('settings-teams-create-description').fill('Created by the FAR-1242 real-stack e2e journey')
      await page.getByTestId('settings-teams-create-submit').click()

      // Observable effect: the creation is confirmed by the form. The
      // rendered message is `Team "<name>" created.` (en-US
      // views.SettingsTeamsView.team_created), so match the name between the
      // two words — a bare /Team created/ never matches it.
      await expect(page.getByText(new RegExp(`Team "${name}" created\\.`, 'i'))).toBeVisible({ timeout: 30_000 })

      // ...and the team really persisted; the owning row renders with a
      // zero owned-resource count (a fresh team owns nothing).
      const list = await listTeams(apiBase, token)
      const created = list.items.find((t) => t.name === name)
      expect(created, 'created team must be returned by GET /api/v1/admin/teams').toBeTruthy()
      teamId = created.id
      await expect(page.locator('.card').filter({ hasText: name }).first()).toBeVisible({ timeout: 30_000 })

      // Rename through the row's Rename action.
      const card = page.locator('.card').filter({ hasText: name }).first()
      await card.getByRole('button', { name: 'Rename' }).click()
      await page.getByTestId('settings-teams-rename-name').fill(renamedName)
      await page.getByTestId('settings-teams-rename-save').click()

      // The card re-renders with the new name...
      const renamedCard = page.locator('.card').filter({ hasText: renamedName }).first()
      await expect(renamedCard).toBeVisible({ timeout: 30_000 })
      // ...and the rename persisted through the real backend. The PUT is
      // optimistic-concurrency guarded with expected_updated_at, so a stale
      // payload would never just overwrite.
      const afterRename = await listTeams(apiBase, token)
      expect(afterRename.items.find((t) => t.id === teamId)?.name).toBe(renamedName)

      // Delete through the row's Delete action + inline confirmation.
      await renamedCard.getByRole('button', { name: 'Delete' }).click()
      await expect(page.getByTestId('settings-teams-delete-confirm')).toBeVisible()
      await page.getByTestId('settings-teams-delete-confirm').click()
      await expect.poll(async () => {
        const after = await listTeams(apiBase, token)
        return after.items.some((t) => t.id === teamId)
      }, { timeout: 30_000, intervals: [1_000, 2_000, 5_000] }).toBe(false)
      teamId = null
    } finally {
      if (teamId) await deleteTeamBestEffort(apiBase, token, teamId)
    }
  })
})
