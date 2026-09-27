import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  deleteSchema,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Real-stack schema journey (FAR-1242 batch 1).
 *
 * Runs against the REAL backend (staging/app); skips the local mock target.
 * The schema registry is the contract layer every agent node validates
 * against, yet schemas.spec.ts only checks that the three tabs render. This
 * journey creates a schema through the editor UI and proves it persisted.
 */

interface SchemaListItem {
  id: string
  name: string
}

test.describe('Real-stack journeys: schema registry', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating a schema in the editor persists it and lists it for browsing', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const schemaName = uniqueName('E2E Journey Schema')
    let createdSchemaId: string | null = null
    try {
      await loginAsAdmin(page, env)

      // Create a schema through the editor UI: new schema -> details -> one
      // field -> save.
      await page.goto('/schemas/editor')
      await page.getByTestId('schema-editor-new').click()
      await page.getByTestId('schema-editor-name').fill(schemaName)
      await page.getByTestId('schema-editor-description').fill('Created by the FAR-1242 real-stack e2e journey')
      await page.getByTestId('schema-editor-add-field').click()
      await page.getByTestId('schema-editor-field-name').fill('approved_output')
      await page.getByTestId('schema-editor-save').click()

      // Observable effect: the editor reports the create...
      await expect(page.getByText('Schema created.')).toBeVisible({ timeout: 30_000 })
      // ...and the sidebar lists the new schema.
      await expect(
        page.getByTestId('schema-editor-list-item').filter({ hasText: schemaName }),
      ).toBeVisible()

      // Persisted: the schema is in the registry through the real backend,
      // which also gives us its id for the browse-tab check and cleanup.
      const listRes = await apiFetch<SchemaListResponse>(apiBase, token, 'GET', '/api/v1/schemas?page_size=100')
      expect(listRes.status).toBe(200)
      const created = listRes.body?.items.find((s) => s.name === schemaName)
      expect(created, 'created schema must be returned by GET /api/v1/schemas').toBeTruthy()
      createdSchemaId = created?.id ?? null

      // The Browse tab renders it too (user-visible registry entry).
      await page.goto('/schemas')
      await expect(page.getByTestId(`schema-row-${createdSchemaId}`)).toBeVisible()
      await expect(page.getByTestId(`schema-row-${createdSchemaId}`)).toContainText(schemaName)
    } finally {
      if (createdSchemaId) await deleteSchema(apiBase, token, createdSchemaId)
    }
  })
})
