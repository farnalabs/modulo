import { test, expect, loginAsAdmin } from '../setup/fixtures'
import { apiBaseFor, apiFetch, apiLogin, uniqueName } from '../setup/realstack-api'

/**
 * Real-stack model backend journey (FAR-1242 batch 1).
 *
 * Runs against the REAL backend (staging/app); skips the local mock target.
 * Creating a backend through the admin UI writes an encrypted credential row
 * through the real backend (the save-time health check against the fake key
 * fails, but the entity persists — the product treats "configured but
 * unreachable" as a normal state). Deepens admin-model-backends.spec.ts,
 * which page.route-mocks the very list it claims to verify.
 */

interface BackendDetail {
  id: string
  name: string
  provider: string
}

test.describe('Real-stack journeys: model backend registration', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('registering a model backend through the admin UI persists it (and it can be removed again)', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const name = uniqueName('e2e-journey-backend')
    let createdBackendId: string | null = null
    try {
      await loginAsAdmin(page, env)
      await page.goto('/admin/model-backends')

      // Open the add form and switch past the presets screen to manual entry.
      await page.getByTestId('admin-model-backends-add').click()
      await page.getByTestId('admin-model-backends-manual-entry-toggle').click()

      await page.getByTestId('admin-model-backends-name-input').fill(name)
      await page.getByTestId('admin-model-backends-display-name-input').fill('E2E Journey Backend')

      // Pick a provider from the select (PrimeVue overlay option).
      await page.getByTestId('admin-model-backends-provider-select').click()
      await page.locator('[data-value="ollama"]').click()
      await page.getByTestId('admin-model-backends-model-id-input').fill('e2e-journey-model')
      await page.getByTestId('admin-model-backends-api-key-input').fill('sk-e2e-journey-not-a-real-key')

      await page.getByTestId('admin-model-backends-submit').click()

      // Observable effect: the new backend row renders in the table...
      const tableRows = page.locator('tr[data-testid^="model-backend-row-"]').filter({ hasText: name })
      await expect(tableRows.first()).toBeVisible({ timeout: 30_000 })

      // ...and the backend really persisted (fetching its id for cleanup).
      const listRes = await apiFetch<{ items: Array<{ id: string; name: string }> }>(apiBase, token, 'GET', '/api/v1/model-backends?page_size=100')
      expect(listRes.status).toBe(200)
      const created = listRes.body?.items.find((b) => b.name === name)
      expect(created, 'created backend must be returned by GET /api/v1/model-backends').toBeTruthy()
      if (!created) throw new Error('[realstack] created backend missing from GET /api/v1/model-backends')
      createdBackendId = created.id

      const detailRes = await apiFetch<BackendDetail>(apiBase, token, 'GET', `/api/v1/model-backends/${created.id}`)
      expect(detailRes.status).toBe(200)
      expect(detailRes.body?.provider).toBe('ollama')

      // Cleanup through the UI seam: remove it again so the shared instance
      // keeps no residue.
      const del = await apiFetch(apiBase, token, 'DELETE', `/api/v1/model-backends/${created.id}`)
      expect(del.status).toBe(204)
      createdBackendId = null

      const goneRes = await apiFetch<BackendDetail>(apiBase, token, 'GET', `/api/v1/model-backends/${created.id}`)
      expect(goneRes.status).toBe(404)
    } finally {
      if (createdBackendId) await apiFetch(apiBase, token, 'DELETE', `/api/v1/model-backends/${createdBackendId}`)
    }
  })
})
