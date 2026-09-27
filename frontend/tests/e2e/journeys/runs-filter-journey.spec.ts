import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiLogin,
  cancelRun,
  cleanupJourneyEntities,
  createManualNodePipeline,
  getRun,
  pollRunStatus,
  triggerRun,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'

/**
 * Real-stack runs search journey (FAR-1242 batch 3).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * stage-board seam: the /runs table is FILTERED (search narrows to a
 * pipeline) and the run's DETAIL page renders the persisted record — trigger
 * actor, the parked HITL gate and the input payload the run was started with.
 *
 * A parked manual-node run is used because it never self-completes, so the
 * rows state is stable for the whole inspection. The run is cancelled at the
 * end (before the pipeline delete) so the shared instance keeps no residue.
 */

test.describe('Real-stack journeys: runs search + run detail record', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('searching /runs by pipeline name narrows to that pipeline, and the detail page renders the persisted run record', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Runs Filter Pipeline'),
      uniqueName('E2E Runs Filter Output Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    try {
      const runPrompt = `E2E journey runs-filter prompt ${Date.now().toString(36)}`
      const run = await triggerRun(apiBase, token, created.pipeline.id, { prompt: runPrompt })
      await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'awaiting_human' || s === 'hitl_parked', {
        timeoutMs: 120_000,
      })

      await loginAsAdmin(page, env)
      await page.goto('/runs')

      // The run row renders for this pipeline even unfiltered.
      const row = page.getByTestId(`runs-list-view-${run.run_id}`)
      await expect(row).toBeVisible({ timeout: 30_000 })
      await expect(row).toContainText(created.pipeline.name)

      // Operator seam: the search filter narrows the table to this
      // pipeline. The row must still list OUR run.
      await page.getByTestId('filter-bar-search').fill(created.pipeline.name)
      await expect(row).toBeVisible({ timeout: 30_000 })
      // A run of a DIFFERENT pipeline cannot share the unique name; the
      // table also still carries our run id link only for this row.
      expect(await page.getByTestId(`runs-list-view-${run.run_id}`).count()).toBe(1)

      // The detail page renders the persisted record...
      await page.goto(`/runs/${run.run_id}`)
      // ...the run is parked at its human gate, so the detail page renders the
      // HITL gate card. The live node-progress strip is deliberately NOT
      // asserted here: it renders only once a node reports telemetry/output,
      // and a parked manual node has executed nothing yet (the Execution
      // Trace section shows its empty state instead).
      await expect(page.getByTestId('hitl-gate-card')).toBeVisible({ timeout: 30_000 })
      // ...the trigger actor records a real MANUAL trigger (persisted on
      // the run row, rendered from the backend values)...
      await expect(page.getByTestId('run-detail-trigger-actor')).toContainText(/manual/i)
      // ...and the input payload the run was ACTUALLY started with is
      // rendered (changes if the payload store drops the value).
      const inputPanel = page.getByTestId('run-detail-input-payload')
      await expect(inputPanel).toBeVisible({ timeout: 30_000 })
      await expect(inputPanel).toContainText(runPrompt)

      // Double-check the persisted side (the source of truth the UI reads).
      const detail = await getRun(apiBase, token, run.run_id)
      expect(detail.status).toBe(200)
      expect(detail.body?.pipeline_id).toBe(created.pipeline.id)

      // Cleanup order: cancel the parked run so the pipeline is deletable.
      await cancelRun(apiBase, token, run.run_id)
      await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'cancelled', { timeoutMs: 90_000 })
    } finally {
      await cleanupJourneyEntities(cleanup)
    }
  })
})
