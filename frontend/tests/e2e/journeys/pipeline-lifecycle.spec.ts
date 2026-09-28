import { type Locator, type Page } from '@playwright/test'
import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  createPipeline,
  deletePipeline,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Invoke a row's action-menu command.
 *
 * The action menu is a PrimeVue popup `<Menu>`. Three independent traps, all
 * observed on staging:
 *
 * 1. PrimeVue 5's `Menuitem` puts `role="menuitem"` on the outer `<li>` but
 *    binds the command handler to the inner `.p-menu-item-content` `<div>`
 *    (primevue/menu/Menuitem.vue). A click that lands on the `<li>` bubbles
 *    up to the overlay's document-level dismisser (closing the menu) but
 *    never reaches the descendant handler — so the command never runs.
 * 2. `dispatchEvent('click')` on that inner `<div>` also fails to run the
 *    command: the synthetic event does not reach PrimeVue's component click
 *    handler (verified against primevue@5.0.1 — the menu stays open and the
 *    command never fires), so a dispatched click silently no-ops.
 * 3. A plain `.click()` on the inner link never completes: the anchored
 *    overlay's enter transition (`p-anchored-overlay`) leaves the `<a>` moving
 *    across frames, so Playwright's actionability wait loops on "element is not
 *    stable" and then "element was detached from the DOM", exhausting the click
 *    timeout (verified in the staging trace for this file — the locator
 *    resolves to `<a class="p-menu-item-link">` but the click never fires).
 *
 * Wait for the item to be visible, then deliver a real pointer click with
 * `force: true`, which skips the stability gate while still dispatching a real
 * mouse event at the element's centre — the bubbling `@click` on
 * `.p-menu-item-content` then runs the command. The item is asserted visible
 * first, so a genuinely missing command still fails.
 */
async function clickRowAction(page: Page, row: Locator, label: string): Promise<void> {
  await row.getByTestId('pipeline-list-action-menu').click()
  const menuItem = page.getByRole('menuitem', { name: label, exact: true })
  await expect(menuItem).toBeVisible({ timeout: 15_000 })
  await menuItem.locator('a.p-menu-item-link').click({ force: true })
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
      await expect(page.getByRole('menuitem', { name: 'Rename', exact: true })).toBeVisible({ timeout: 15_000 })
      const deleteItem = page.getByRole('menuitem', { name: 'Delete', exact: true })
      if ((await deleteItem.count()) === 0) {
        test.skip(true, 'pipeline_delete is not enabled on this deployment')
      }
      await deleteItem.locator('a.p-menu-item-link').click({ force: true })
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
