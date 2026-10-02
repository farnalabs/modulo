import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiLogin,
  cleanupJourneyEntities,
  createManualNodePipeline,
  pollRunStatus,
  reissueApproveBestEffort,
  triggerRun,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'

/**
 * Real-stack run/HITL/audit journey (FAR-1242 batch 1) — the golden-path
 * execution spine without an LLM: a one-node "manual" pipeline parks the run
 * at a human-input gate (LangGraph interrupt), and the run completes only
 * after a human claims and approves the gate through the UI. The decision is
 * then visible in the audit log.
 *
 * Runs against the REAL backend (staging/app); skips the local mock target.
 * No `page.route` mocking anywhere — the endpoints under test are hit for
 * real. The journey creates its own pipeline + schema and deletes both.
 *
 * Deepens sse-crossworker-notification.spec.ts's seam (run events) and covers
 * the run-detail/HITL surface that no other e2e spec exercises for real.
 */

const isParked = (status: string) => status === 'awaiting_human' || status === 'hitl_parked'

test.describe('Real-stack journeys: run parks at HITL and completes on approval', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('running a manual-node pipeline parks the run at its human-input gate', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Manual Run'),
      uniqueName('E2E Manual Output Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    try {
      await loginAsAdmin(page, env)

      // Run through the editor's real run dialog (saves the graph and
      // triggers the run through POST /api/v1/runs). The editor gates its
      // toolbar on a catalog fetch, so wait on the staging readiness budget
      // rather than the default 5 s expect budget (a staging @regression run
      // flaked this assertion while the graph/catalog were still in flight).
      await page.goto(`/pipelines/${created.pipeline.id}/editor`)
      await expect(page.getByTestId('pipeline-editor-run')).toBeEnabled({ timeout: 30_000 })
      await page.getByTestId('pipeline-editor-run').click()
      await page.getByTestId('pipeline-editor-run-prompt').fill('E2E journey run prompt')
      await page.getByTestId('pipeline-editor-run-submit').click()

      // The editor routes to the run it created (PipelineEditorView reads
      // `run_id` from POST /api/v1/runs, fixed by FAR-1246/#1013) — this
      // waitForURL IS the end-to-end regression pin for that fix.
      await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/i, { timeout: 30_000 })
      const runId = new URL(page.url()).pathname.split('/').pop() as string
      expect(runId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)

      // Observable effect: the run parks and its detail page renders the open
      // gate — the review card exists only while a human decision is pending.
      await page.goto(`/runs/${runId}`)
      await expect(page.getByTestId('hitl-gate-card')).toBeVisible({ timeout: 90_000 })

      // Persisted state: the backend reports the run parked at the gate.
      const status = await pollRunStatus(apiBase, token, runId, isParked, { timeoutMs: 90_000 })
      expect(isParked(status)).toBe(true)
    } finally {
      await cleanupJourneyEntities(cleanup)
    }
  })

  test('claiming and approving the gate completes the run, lists it, and audits the decision', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey HITL Approve'),
      uniqueName('E2E Manual Output Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    try {
      // Trigger the run through the real API and wait for it to park.
      const run = await triggerRun(apiBase, token, created.pipeline.id, { prompt: 'E2E journey approval run' })
      await pollRunStatus(apiBase, token, run.run_id, isParked, { timeoutMs: 120_000 })

      await loginAsAdmin(page, env)
      await page.goto(`/runs/${run.run_id}`)
      await expect(page.getByTestId('hitl-gate-card')).toBeVisible({ timeout: 30_000 })

      // Claim the gate, then approve with a note — the exact review actions
      // the product exists to govern.
      await page.getByTestId('hitl-gate-claim').click()
      await expect(page.getByTestId('hitl-gate-approve')).toBeVisible({ timeout: 15_000 })
      await page.getByTestId('hitl-gate-notes').fill('E2E journey approval')
      await page.getByTestId('hitl-gate-approve').click()

      // The run must really resume and complete on the backend. Staging's DB
      // and backend can transiently 503 mid-approve, which leaves the run
      // parked at its still-claimed gate. Re-issue the decision in a bounded
      // loop until the run completes. Recovery MUST NOT go through the UI
      // approve control: while the first approve request is still in flight
      // that control is disabled ("Approving…"), so clicking it blocks until
      // the test timeout instead of letting the loop retry. Re-issue through
      // the real API instead (same-account re-claim re-issues a fresh token,
      // FAR-686), which works regardless of the UI button's state.
      //
      // BOUND the re-issue phase by a total DEADLINE, not a fixed iteration
      // count: a retry loop sized only by iterations runs for (iterations ×
      // poll budget), which overran Playwright's 120 s test timeout on
      // staging (2026-10-01) — the run had in fact completed while the loop
      // kept trying, and the test died on the deadline with a misleading
      // "did not complete" message. The deadline below leaves the suite ~30 s
      // of headroom, and the final status is re-observed after the loop so a
      // run that completed during the last re-issue still passes.
      let status = ''
      let lastError: unknown
      const approveDeadline = Date.now() + 90_000
      while (status !== 'complete' && Date.now() < approveDeadline) {
        try {
          status = await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'complete', { timeoutMs: 15_000 })
        } catch (err) {
          lastError = err
          await reissueApproveBestEffort(apiBase, token, run.run_id, 'E2E journey approval')
        }
      }
      if (status !== 'complete') {
        // Last chance: the run may have completed during the final re-issue
        // (the sub-budget poll above was capped at 15 s), so re-observe once
        // with the remaining headroom before failing. A genuinely wedged run
        // still fails — this only removes the false negative at the boundary.
        try {
          status = await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'complete', { timeoutMs: 25_000 })
        } catch (err) {
          lastError = err
        }
      }
      if (status !== 'complete') {
        throw new Error(
          `run ${run.run_id} did not complete after re-issuing the approve decision; ` +
            `last poll: ${lastError instanceof Error ? lastError.message : String(lastError)}`,
        )
      }
      expect(status).toBe('complete')

      // Observable effect: the decision is hoisted as success feedback...
      await expect(page.getByTestId('run-detail-hitl-message')).toBeVisible({ timeout: 15_000 })

      // The completed run is listed on /runs with its pipeline name.
      await page.goto('/runs')
      const runLink = page.getByTestId(`runs-list-view-${run.run_id}`)
      await expect(runLink).toBeVisible({ timeout: 30_000 })
      await expect(runLink).toContainText(created.pipeline.name)

      // The claim/decision left a real trace in the audit log (the audit
      // logger writes hitl_claimed + the decision event for this run).
      await page.goto('/admin/audit')
      const hitlEvents = page.locator('tr[data-testid^="admin-audit-event-row-"]').filter({ hasText: /hitl/i })
      await expect(hitlEvents.first()).toBeVisible({ timeout: 30_000 })
    } finally {
      await cleanupJourneyEntities(cleanup)
    }
  })

  // NOTE (FAR-1242 batch 1): a reject journey was considered here and
  // deliberately NOT written. Rejecting a manual-input node injects
  // {"action": "rejected"} with no "output", so the node resumes with
  // manual_output=None and the run COMPLETES (reject acts as skip for manual
  // nodes). Pinning that would duplicate the approve journey's terminal
  // assertion; a reject journey with real fail-closed semantics needs a
  // reject_target edge/HITL gate graph and belongs to a later batch.
})
