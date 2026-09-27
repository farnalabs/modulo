import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  deleteLifecycleMapBestEffort,
  getLifecycleMap,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Real-stack lifecycle-map journey (FAR-1242 batch 3).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * library/lifecycle-map seam no other batch touches: a map is CREATED through
 * the list page's real create dialog, a stage graph is persisted through the
 * backend's map update route, the detail page renders the persisted graph,
 * and the map is DELETED again through the detail page's real delete dialog.
 *
 * Every observable effect is checked against the backend as well as the UI:
 * the persisted row, the normalised stage graph, and the eventual 404.
 * The map is deleted in a finally block, so a failed assertion cannot leave
 * residue on the shared instance.
 */

const TWO_STAGE_CONTENT = {
  stages: [
    { id: 'e2e-stage-ide', name: 'E2E Journey Stage IDE', type: 'external' },
    { id: 'e2e-stage-build', name: 'E2E Journey Stage Build', type: 'manual' },
  ],
  edges: [
    {
      id: 'e2e-edge-build',
      source: 'e2e-stage-ide',
      target: 'e2e-stage-build',
      label: 'handoff',
    },
  ],
}

test.describe('Real-stack journeys: lifecycle map lifecycle', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating a lifecycle map through the list UI persists it, the stage graph persists, and deleting drops the row', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const mapName = uniqueName('E2E Journey Lifecycle Map')
    let mapId: string | null = null

    try {
      await loginAsAdmin(page, env)

      // CREATE through the real list-page dialog.
      await page.goto('/lifecycle-maps')
      await page.getByTestId('lifecycle-map-list-new').click()
      const dialog = page.locator('div.fixed.inset-0 div.w-full.max-w-md')
      await expect(dialog).toBeVisible({ timeout: 30_000 })
      await dialog.locator('#lifecyclemaplist-field-2').fill(mapName)
      await dialog.locator('#lifecyclemaplist-field-1').fill('Created by the FAR-1242 real-stack e2e journey')
      await dialog.getByRole('button', { name: 'Create' }).click()

      // The dialog pushes straight into the graph editor; its toolbar
      // heading re-renders the persisted map name (backed by a real fetch).
      await expect(page.locator('h2').filter({ hasText: mapName })).toBeVisible({ timeout: 30_000 })

      // Resolve the created map from the real API (uniquely named).
      const list = await apiFetch<{ items: Array<{ id: string; name: string }> }>(
        apiBase,
        token,
        'GET',
        '/api/v1/lifecycle-maps?page_size=100',
      )
      expect(list.status).toBe(200)
      const created = list.body?.items.find((m) => m.name === mapName)
      expect(created, 'created lifecycle map must be returned by GET /api/v1/lifecycle-maps').toBeTruthy()
      mapId = created?.id ?? null
      if (!mapId) throw new Error('[realstack] created lifecycle map id could not be resolved')

      // A fresh map has an EMPTY stage graph.
      const before = await getLifecycleMap(apiBase, token, mapId)
      expect(before.status).toBe(200)
      expect(before.body?.stages).toEqual([])

      // Persist a two-stage graph through the backend's map update route —
      // the normalisation path the editor save takes. An ill-shaped stage
      // would be refused here with a 422, never silently stored.
      const put = await apiFetch(apiBase, token, 'PUT', `/api/v1/lifecycle-maps/${mapId}`, {
        name: mapName,
        content_json: TWO_STAGE_CONTENT,
      })
      expect(put.status).toBe(200)

      // Persisted: the backend reports both stages back.
      const afterSave = await getLifecycleMap(apiBase, token, mapId)
      expect(afterSave.status).toBe(200)
      expect(afterSave.body?.stages).toHaveLength(2)
      const stageNames = (afterSave.body?.stages ?? []).map((s) => s.name as string)
      expect(stageNames).toContain('E2E Journey Stage IDE')
      expect(stageNames).toContain('E2E Journey Stage Build')

      // The detail page renders the PERSISTED graph data (h1 title from the
      // fetched map, "2 stages" computed from the stored stages array).
      await page.goto(`/lifecycle-maps/${mapId}`)
      await expect(page.locator('h1').filter({ hasText: mapName })).toBeVisible({ timeout: 30_000 })
      await expect(page.getByText('2 stages')).toBeVisible()

      // DELETE through the detail page's real delete dialog.
      await page.getByTestId('lifecycle-map-view-delete').click()
      const deleteDialog = page.getByRole('dialog').filter({ hasText: /delete lifecycle map/i })
      await expect(deleteDialog).toBeVisible()
      await deleteDialog.getByRole('button', { name: 'Delete' }).click()

      // Observable effect: the backend really dropped the record (404).
      const deletedId = mapId
      mapId = null
      await expect.poll(async () => (await getLifecycleMap(apiBase, token, deletedId)).status, {
        timeout: 30_000,
        intervals: [1_000, 2_000, 5_000],
      }).toBe(404)
    } finally {
      if (mapId) await deleteLifecycleMapBestEffort(apiBase, token, mapId)
    }
  })
})
