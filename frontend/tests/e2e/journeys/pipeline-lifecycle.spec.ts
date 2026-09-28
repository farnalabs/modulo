import { type Locator, type Page } from '@playwright/test'
import { test, expect, loginAsAdmin } from '../setup/fixtures'
import { clickMenuItem } from '../setup/row-menu'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  createPipeline,
  deletePipeline,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Invoke a row's action-menu command. See `setup/row-menu.ts` for why a real
 * pointer sequence is required (the overlay transition defeats coordinate
 * clicks, and a synthetic click lands on the item's anchor href and navigates).
 */
async function clickRowAction(page: Page, row: Locator, label: string): Promise<void> {
  await row.getByTestId('pipeline-list-action-menu').click()
  await clickMenuItem(page, page.getByRole('menuitem', { name: label, exact: true }))
}

/**
 * Real-stack pipeline lifecycle journeys (FAR-1242 batch 1).
 *
 * These run against the REAL backend of the target (staging/app) and skip the
 * local target, where the whole API is page.route-mocked by setupLocalMockApi.
 * Every journey creates its own pipeline with a unique name and deletes it
 * again, so it is safe on a shared instance.
 *
 * Deepens pipelines.spec.ts, whose data-dependent tests skip on non-local
 * targets and whose assertions stop at "input keeps its typed value".
 */

interface PipelineDetail {
  id: string
  name: string
  archived_at: string | null
}

test.describe('Real-stack journeys: pipeline lifecycle', { tag: '@regression' }, () => {
  // The Assistant floating panel (rendered where dev-mode is on) opens by
  // default and its fixed-position overlay intercepts clicks. Force it closed
  // before the app boots. Mirrors pipeline-editor.spec.ts.
  test.beforeEach(async ({ page, env }) => {
    // Real-stack only: these journeys need a real backend.
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('renaming a pipeline from the list persists the new name', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const name = uniqueName('E2E Journey Rename')
    const renamed = uniqueName('E2E Journey Renamed')
    const pipeline = await createPipeline(apiBase, token, name)
    try {
      await loginAsAdmin(page, env)
      await page.goto('/pipelines')

      // The created pipeline is listed (persisted data rendering, not client state).
      const row = page.getByTestId(`pipeline-tree-row-${pipeline.id}`)
      await expect(row).toBeVisible()
      await expect(row).toContainText(name)

      // Rename via the row's action menu.
      await clickRowAction(page, row, 'Rename')
      const dialog = page.locator('dialog').filter({ has: page.locator('#pipelinelistview-field-1') })
      await expect(dialog).toBeVisible()
      await dialog.locator('#pipelinelistview-field-1').fill(renamed)
      await dialog.getByRole('button', { name: 'Save' }).click()
      await expect(dialog).toHaveCount(0)

      // Observable effect: the row re-renders with the new name...
      await expect(row).toContainText(renamed)
      // ...and the rename persisted through the real backend.
      const res = await apiFetch<PipelineDetail>(apiBase, token, 'GET', `/api/v1/pipelines/${pipeline.id}`)
      expect(res.status).toBe(200)
      expect(res.body?.name).toBe(renamed)
    } finally {
      await deletePipeline(apiBase, token, pipeline.id)
    }
  })

  test('archiving a pipeline from the list removes it and persists the archived state', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const name = uniqueName('E2E Journey Archive')
    const pipeline = await createPipeline(apiBase, token, name)
    try {
      await loginAsAdmin(page, env)
      await page.goto('/pipelines')

      const row = page.getByTestId(`pipeline-tree-row-${pipeline.id}`)
      await expect(row).toBeVisible()

      // Archive via the row's action menu.
      await clickRowAction(page, row, 'Archive')

      // Observable effect: the default list excludes archived pipelines, so
      // the row disappears for the user...
      await expect(row).toHaveCount(0)
      // ...and the archived state persisted through the real backend.
      const res = await apiFetch<PipelineDetail>(apiBase, token, 'GET', `/api/v1/pipelines/${pipeline.id}`)
      expect(res.status).toBe(200)
      expect(res.body?.archived_at).toBeTruthy()

      // The reverse seam: unarchive restores the pipeline to the list.
      const un = await apiFetch<PipelineDetail>(apiBase, token, 'POST', `/api/v1/pipelines/${pipeline.id}/unarchive`)
      expect(un.status).toBe(200)
      expect(un.body?.archived_at).toBeNull()
      await page.goto('/pipelines')
      await expect(page.getByTestId(`pipeline-tree-row-${pipeline.id}`)).toBeVisible()
    } finally {
      await deletePipeline(apiBase, token, pipeline.id)
    }
  })

  test('an empty pipeline cannot run: the UI disables Run and the backend refuses the trigger', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const name = uniqueName('E2E Journey Empty Graph')
    const pipeline = await createPipeline(apiBase, token, name)
    try {
      await loginAsAdmin(page, env)
      await page.goto(`/pipelines/${pipeline.id}/editor`)
      await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible()

      // UI guard: Run is disabled while the graph has no nodes.
      await expect(page.getByTestId('pipeline-editor-run')).toBeDisabled()

      // Backend seam: triggering the empty pipeline through the real API is
      // refused with a validation error (POST /api/v1/runs -> 422, not a 500).
      const res = await apiFetch<{ detail: unknown }>(apiBase, token, 'POST', '/api/v1/runs', {
        pipeline_id: pipeline.id,
        input_payload: {},
      })
      expect(res.status).toBe(422)
      expect(JSON.stringify(res.body)).toContain('no nodes')
    } finally {
      await deletePipeline(apiBase, token, pipeline.id)
    }
  })

  test('deleting a pipeline from the list removes it and the backend drops it', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const name = uniqueName('E2E Journey Delete')
    const pipeline = await createPipeline(apiBase, token, name)
    // No finally-delete here: the test itself deletes the pipeline. The
    // fallback below only runs if the UI delete never happened.
    let deletedThroughUi = false
    try {
      await loginAsAdmin(page, env)
      await page.goto('/pipelines')

      const row = page.getByTestId(`pipeline-tree-row-${pipeline.id}`)
      await expect(row).toBeVisible()

      // Delete via the row's action menu. pipeline_delete is a team-tier
      // feature flag; when the plan store cannot resolve flags for this
      // account the command is absent, so skip rather than fail the suite on
      // an unavailable surface.
      await row.getByTestId('pipeline-list-action-menu').click()
      const deleteItem = page.getByRole('menuitem', { name: 'Delete', exact: true })
      await expect(deleteItem).toBeVisible({ timeout: 15_000 })
      if ((await deleteItem.count()) === 0) {
        test.skip(true, 'pipeline_delete is not enabled on this deployment')
      }
      await clickMenuItem(page, deleteItem)
      const dialog = page.locator('dialog').filter({ hasText: 'Delete Pipeline' })
      await expect(dialog).toBeVisible()
      await dialog.getByRole('button', { name: 'Delete' }).click()
      deletedThroughUi = true

      // Observable effect: the row is gone from the list...
      await expect(row).toHaveCount(0)
      // ...and the pipeline is really gone from the backend.
      const res = await apiFetch<PipelineDetail>(apiBase, token, 'GET', `/api/v1/pipelines/${pipeline.id}`)
      expect(res.status).toBe(404)
    } finally {
      if (!deletedThroughUi) await deletePipeline(apiBase, token, pipeline.id)
    }
  })
})
