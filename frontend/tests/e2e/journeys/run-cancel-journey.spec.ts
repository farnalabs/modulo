import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  cleanupJourneyEntities,
  createManualNodePipeline,
  getRun,
  pollRunStatus,
  triggerRun,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'
/**
 * Real-stack run-cancel journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. A
 * parked manual-node run is cancelled from the runs list — the operator seam —
 * and the persisted record must say WHY (user_requested) and WHO (the acting
 * account), not just flip to a terminal status.
 *
 * The cancelled run is terminal, so it cannot be re-run accidentally; the
 * pipeline and schema are deleted in a finally block.
 */

test.describe('Real-stack journeys: run cancellation', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('cancelling a parked run records why and who cancelled it', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Cancel Run'),
      uniqueName('E2E Cancel Output Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    try {
      // Park a run at the manual gate through the real API.
      const run = await triggerRun(apiBase, token, created.pipeline.id, { prompt: 'E2E journey cancel run' })
      await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'awaiting_human' || s === 'hitl_parked', { timeoutMs: 120_000 })

      // Resolve the acting account (cancelled_by must be this principal).
      const meRes = await apiFetch<{ id: string }>(apiBase, token, 'GET', '/api/v1/me')
      expect(meRes.status).toBe(200)
      const adminAccountId = meRes.body?.id

      await loginAsAdmin(page, env)
      await page.goto('/runs')

      // Operator seam: cancel from the list row. Cancellation is the runs
      // list's two-step in-row confirm (mirrors rerun): the first click only
      // arms the control — its label flips from "Stop" to "Confirm?" — and
      // the second click commits the cancel request. A single click leaves the
      // run parked at the gate, so the button must be clicked twice.
      const cancelButton = page.getByTestId(`runs-list-cancel-${run.run_id}`)
      await expect(cancelButton).toBeVisible({ timeout: 30_000 })
      await cancelButton.click()
      await expect(cancelButton).toContainText(/confirm/i, { timeout: 10_000 })
      await cancelButton.click()

      // Persisted: the run really terminalised as cancelled...
      const status = await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'cancelled', { timeoutMs: 90_000 })
      expect(status).toBe('cancelled')

      // ...with the closed-vocabulary reason and the acting account recorded.
      const detail = await getRun(apiBase, token, run.run_id)
      expect(detail.status).toBe(200)
      expect(detail.body?.status).toBe('cancelled')
      expect(detail.body?.cancel_reason).toBe('user_requested')
      expect(detail.body?.cancelled_by).toBe(adminAccountId)

      // The detail page renders the cancellation record (reason + actor).
      await page.goto(`/runs/${run.run_id}`)
      await expect(page.getByTestId('run-detail-cancel-reason')).toBeVisible({ timeout: 30_000 })
      await expect(page.getByTestId('run-detail-cancel-reason')).toContainText(/operator cancelled/i)
      await expect(page.getByTestId('run-detail-cancelled-by')).toBeVisible()
    } finally {
      await cleanupJourneyEntities(cleanup)
    }
  })
})
