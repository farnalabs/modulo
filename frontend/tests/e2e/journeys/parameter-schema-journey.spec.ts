import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  deleteParameterSchemaBestEffort,
  hasParameterSchemaAccess,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Real-stack parameter-schema journey (FAR-1242 batch 3).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * admin configure surface: a parameter schema with one string parameter is
 * created through the real admin form, a parameter set is saved with an
 * exact value for that parameter through the set editor, both deletes are
 * driven through the real UI, and every step is verified through the
 * backend.
 *
 * The set's persisted VALUES are the strongest observable here: the stored
 * record carries exactly the value an operator typed, which changes if the
 * code under test drops or mangles it.
 *
 * Deleted in a finally block, so a failed assertion cannot leave residue.
 */

test.describe('Real-stack journeys: parameter schema + set', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating a parameter schema and a set through the admin UI persists the exact values', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)

    // Precondition: the Team-tier surface is exposed on this target.
    const accessible = await hasParameterSchemaAccess(apiBase, token)
    test.skip(!accessible, 'parameter schemas are unavailable on this target (Team-tier feature absent)')

    const schemaName = uniqueName('e2e-journey-param-schema')
    const paramName = 'e2e_model_id'
    const setName = uniqueName('e2e-journey-param-set')
    let schemaId: string | null = null

    try {
      await loginAsAdmin(page, env)
      await page.goto('/admin/parameter-schemas')

      // CREATE through the real form: name + one string parameter.
      await page.getByTestId('paramschema-new').click()
      await page.getByTestId('paramschema-name-input').fill(schemaName)
      await page.getByTestId('paramschema-desc-input').fill('Created by the FAR-1242 real-stack e2e journey')
      await page.getByRole('button', { name: 'Add Parameter' }).click()
      const paramBlock = page.getByTestId('paramschema-param-0')
      await expect(paramBlock).toBeVisible()
      await paramBlock.locator('#paramschema-param-name-0').fill(paramName)
      await paramBlock.locator('#paramschema-param-label-0').fill('E2E Journey Param')
      await page.getByRole('button', { name: 'Create' }).click()

      // Observable effect: the creation is confirmed by the form...
      await expect(page.getByText('Schema created successfully.')).toBeVisible({ timeout: 30_000 })

      // ...and the schema + parameter really persisted.
      const list = await apiFetch<{
        items: Array<{ id: string; name: string; parameters: Array<Record<string, unknown>> }>
      }>(apiBase, token, 'GET', '/api/v1/parameter-schemas?page=1&page_size=100')
      expect(list.status).toBe(200)
      const created = list.body?.items.find((s) => s.name === schemaName)
      expect(created, 'created parameter schema must be returned by GET /api/v1/parameter-schemas').toBeTruthy()
      schemaId = created?.id ?? null
      if (!schemaId) throw new Error('[realstack] created parameter schema id could not be resolved')
      expect((created?.parameters ?? []).length).toBe(1)
      expect((created?.parameters ?? [])[0]?.name).toBe(paramName)

      // NEW SET through the real set editor: name + the exact operator value.
      await page.getByRole('tab', { name: 'Parameter Sets' }).click()
      await page.getByTestId('paramschema-new-set').click()
      await page.getByTestId('paramschema-set-name').fill(setName)
      await page.locator(`#paramschema-set-value-${paramName}`).fill('e2e-journey-value')
      await page.getByTestId('paramschema-set-save').click()

      // Persisted: the backend lists the set with the exact stored value.
      await expect.poll(async () => {
        const res = await apiFetch<Array<{ id: string; name: string; values: Record<string, unknown> }>>(
          apiBase,
          token,
          'GET',
          `/api/v1/parameter-schemas/${schemaId}/sets`,
        )
        if (res.status !== 200) return null
        return (res.body ?? []).find(
          (s) => s.name === setName && s.values?.[paramName] === 'e2e-journey-value',
        ) ?? null
      }, { timeout: 30_000, intervals: [1_000, 2_000, 5_000] }).toBeTruthy()

      // DELETE the set through the real UI seam (the sets list refreshes
      // through the real backend after the confirm).
      await page.getByTestId('paramschema-delete-set').first().click()
      const setConfirm = page.getByTestId('paramschema-delete-set-confirm')
      await expect(setConfirm).toBeVisible()
      await setConfirm.getByRole('button', { name: 'Delete' }).click()
      await expect.poll(async () => {
        const res = await apiFetch<Array<{ id: string; name: string }>>(
          apiBase,
          token,
          'GET',
          `/api/v1/parameter-schemas/${schemaId}/sets`,
        )
        return (res.body ?? []).some((s) => s.name === setName)
      }, { timeout: 30_000, intervals: [1_000, 2_000, 5_000] }).toBe(false)

      // Back to the schema list and delete through the row's real flow.
      await page.getByTestId('paramschema-back').click()
      const schemaRow = page.locator('tr').filter({ hasText: schemaName }).first()
      await expect(schemaRow).toBeVisible({ timeout: 30_000 })
      await schemaRow.getByTestId('paramschema-delete').click()
      const schemaConfirm = page.getByTestId('paramschema-delete-confirm')
      await expect(schemaConfirm.getByRole('button', { name: 'Delete' })).toBeEnabled()
      await schemaConfirm.getByRole('button', { name: 'Delete' }).click()

      // Persisted: the schema really dropped out of the backend list.
      const createdId = created?.id ?? null
      if (!createdId) throw new Error('[realstack] created parameter schema id could not be resolved')
      await expect.poll(async () => {
        const res = await apiFetch<{ items: Array<{ id: string }> }>(
          apiBase,
          token,
          'GET',
          '/api/v1/parameter-schemas?page=1&page_size=100',
        )
        return (res.body?.items ?? []).some((s) => s.id === createdId)
      }, { timeout: 30_000, intervals: [1_000, 2_000, 5_000] }).toBe(false)
      schemaId = null
    } finally {
      if (schemaId) await deleteParameterSchemaBestEffort(apiBase, token, schemaId)
    }
  })
})
