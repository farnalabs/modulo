import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  claimGate,
  cleanupJourneyEntities,
  createManualNodePipeline,
  getRunIo,
  getRunPendingGates,
  pollRunStatus,
  triggerRun,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'

/**
 * Real-stack run-IO journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * manual-output round trip is the seam where human input becomes run state:
 * the reviewer's delivered payload is validated against the node's output
 * schema, persisted into the run's outputs, and surfaced by the per-node IO
 * inspection endpoint. Batch 1 proved approve completes a run; this journey
 * proves the DELIVERED VALUE lands in the run's persisted IO.
 *
 * No page.route mocking anywhere; everything self-cleans.
 */

const isParked = (status: string) => status === 'awaiting_human' || status === 'hitl_parked'

test.describe('Real-stack journeys: manual output lands in run IO', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('delivering manual output at the gate resumes the run and persists the value in run IO', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Manual IO'),
      uniqueName('E2E Manual IO Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    try {
      const run = await triggerRun(apiBase, token, created.pipeline.id, { prompt: 'E2E journey manual-io run' })
      await pollRunStatus(apiBase, token, run.run_id, isParked, { timeoutMs: 120_000 })

      // Resolve the parked gate (for a manual node the gate id is the node id).
      const gates = await getRunPendingGates(apiBase, token, run.run_id)
      const gate = gates.find((g) => g.decision === null)
      expect(gate, 'the parked run must report one undecided gate').toBeTruthy()
      const gateId = gate?.gate_id ?? ''

      // Claim through the real API and deliver a distinctive output.
      const claimToken = await claimGate(apiBase, token, run.run_id, gateId)
      const deliveredValue = `e2e-manual-${crypto.randomUUID().slice(0, 8)}`
      const submitRes = await apiFetch(apiBase, token, 'POST', `/api/v1/runs/${run.run_id}/manual/${gateId}/submit`, {
        claim_token: claimToken,
        output: { approved_output: deliveredValue },
      })
      expect(submitRes.status).toBe(200)

      // The run really resumed and completed on the backend.
      const status = await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'complete', { timeoutMs: 120_000 })
      expect(status).toBe('complete')

      // Persisted: the delivered value is in the run's per-node IO — the
      // normalized outputs carry the manual node's validated return.
      const ioRes = await getRunIo(apiBase, token, run.run_id)
      expect(ioRes.status).toBe(200)
      const nodeOutput = (ioRes.body?.outputs_json ?? {})[gateId] as Record<string, unknown> | undefined
      expect(nodeOutput, 'the manual node output must appear in the run IO').toBeTruthy()
      expect((nodeOutput as Record<string, unknown>)?.approved_output).toBe(deliveredValue)

      // The detail page renders the run we drove (input payload we sent).
      await loginAsAdmin(page, env)
      await page.goto(`/runs/${run.run_id}`)
      const inputPanel = page.getByTestId('run-detail-input-payload')
      await expect(inputPanel).toBeVisible({ timeout: 30_000 })
      await expect(inputPanel).toContainText('E2E journey manual-io run')
    } finally {
      await cleanupJourneyEntities(cleanup)
    }
  })
})
