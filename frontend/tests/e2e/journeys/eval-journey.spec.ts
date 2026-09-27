import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  createPipeline,
  deleteEvalBestEffort,
  deletePipeline,
  listEvals,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Real-stack eval-definition journey (FAR-1242 batch 3).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * evals/editor seam: an eval definition is CREATED for a real pipeline
 * through the editor's form (POST /api/v1/evals, admin-gated), the editor's
 * list re-renders it, the backend lists it again, and the UI delete seam
 * drops it for good.
 *
 * Persisting an eval definition writes no model calls and no runs — a
 * definition is configuration the engine later READS, so the journey is
 * deterministic and free on the shared instance. Cleanup deletes the eval
 * (and the pipeline) inside finally.
 *
 * Precondition: the instance exposes the eval surface (GET /evals 200);
 * otherwise the whole journey is skipped with a reason.
 */

test.describe('Real-stack journeys: eval definition lifecycle', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating an eval definition through the editor persists it, and deleting removes it for good', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const pipelineName = uniqueName('E2E Journey Eval Pipeline')
    let pipelineId: string | null = null
    let evalId: string | null = null
    let deletedThroughUi = false

    try {
      const pipeline = await createPipeline(apiBase, token, pipelineName)
      pipelineId = pipeline.id

      // Precondition: the eval surface is reachable in this plan tier.
      const precheck = await apiFetch(apiBase, token, 'GET', `/api/v1/evals?pipeline_id=${pipeline.id}`)
      test.skip(precheck.status !== 200, `eval surface unavailable on this target (GET /api/v1/evals -> ${precheck.status})`)

      await loginAsAdmin(page, env)
      await page.goto('/evals/editor')

      // Select OUR pipeline through the real select.
      await page.getByTestId('eval-editor-pipeline').click()
      await page.locator(`[data-value="${pipeline.id}"]`).first().click()

      // Create through the real form: name + the lightweight regex type
      // (the default llm_judge judge-config placeholder stays untouched).
      const evalName = uniqueName('E2E Journey EVAL_N')
      await page.getByTestId('eval-editor-name').fill(evalName)
      await page.getByTestId('eval-editor-eval-type').click()
      await page.locator('[data-value="regex"]').click()
      await page.getByTestId('eval-editor-config').fill('{}')
      await page.getByTestId('eval-editor-save').click()

      // Observable effect: the editor confirms the creation and its list
      // re-renders the persisted definition.
      await expect(page.getByText(/Eval created\./i)).toBeVisible({ timeout: 30_000 })
      const evalCard = page.locator('.rounded-lg.bg-card').filter({ hasText: evalName }).first()
      await expect(evalCard).toBeVisible({ timeout: 30_000 })
      // The card carries the persisted_TYPE badge from the backend shape
      // (a regex definition, not anything the user typed in).
      await expect(evalCard).toContainText('regex')

      // Persisted: the backend lists the new definition for this pipeline.
      const afterCreate = await listEvals(apiBase, token, pipeline.id)
      const created = afterCreate.items.find((e) => e.name === evalName)
      expect(created, 'created eval must be returned by GET /api/v1/evals').toBeTruthy()
      expect(created?.eval_type).toBe('regex')
      evalId = created?.id ?? null
      if (!evalId) throw new Error('[realstack] created eval id could not be resolved')

      // Delete through the editor's real delete flow.
      await evalCard.getByTestId('eval-editor-delete').click()
      await evalCard.getByTestId('eval-editor-confirm-delete').click()
      deletedThroughUi = true

      // The list no longer renders the deleted eval...
      await expect(evalCard).toHaveCount(0)
      // ...and the backend list dropped it for good.
      await expect.poll(async () => {
        const remaining = await listEvals(apiBase, token, pipeline.id)
        return remaining.items.some((e) => e.id === evalId)
      }, { timeout: 30_000, intervals: [1_000, 2_000, 5_000] }).toBe(false)
      evalId = null
    } finally {
      if (evalId && !deletedThroughUi) await deleteEvalBestEffort(apiBase, token, evalId)
      if (pipelineId) await deletePipeline(apiBase, token, pipelineId)
    }
  })
})
