import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiLogin,
  cleanupJourneyEntities,
  createManualNodePipeline,
  pollRunStatus,
  triggerRun,
  uniqueName,
  waitForRunCompletionWithHitlRecovery,
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
    // The recovery wait below is bounded by a 180 s deadline, but that budget
    // is spent inside the shared test/beforeEach-hook timeout and the test
    // timeout BEGINS at hook start (Playwright: the test timeout is shared
    // with beforeEach). This test's beforeEach logs in and runs the
    // afterEach-style cleanup, so the default 180 s can be consumed by the
    // hook before the deadline is honoured. Extend the timeout so deadline +
    // teardown always fit: the pre-recovery setup (including a park poll
    // bounded at 120 s), the 180 s recovery, the post-recovery UI/audit
    // assertions and teardown.
    test.setTimeout(540_000)
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
      // parked at its still-claimed gate (the UI approve 503s, and a recovery
      // attempt's own approve 503s AFTER its claim has already flipped the run
      // to `claimed`). The outage is not always a single blip: the 2026-10-02
      // staging deploy saw an unbroken 503 storm for ~4.5 minutes, which
      // outlasted the previous 90 s-per-attempt budget once Playwright's two
      // retries were spent. Re-issue the decision through the real API until
      // the run completes; the bounded recovery loop and its rationale live in
      // waitForRunCompletionWithHitlRecovery. It is deadline-bounded (never a
      // fixed iteration count, whose worst case overran the hook timeout), and
      // its own re-issue — NOT the committed-decision reconcile, which skips a
      // claimed-but-undecided row unconditionally — is what carries that run to
      // completion rather than reporting it as a hard failure. Size each
      // attempt (180 s) so a single attempt rides out a multi-minute outage;
      // Playwright's two retries stack three such budgets (~9 min total).
      const outcome = await waitForRunCompletionWithHitlRecovery(apiBase, token, run.run_id, {
        deadlineMs: 180_000,
        notes: 'E2E journey approval',
      })
      if (outcome.kind === 'infra-blocked') {
        // Every re-issue over the whole bounded window failed transiently —
        // staging's DB was 503ing the approve transaction for minutes
        // (observed 2026-10-02: an unbroken ~9 min storm across the suite's
        // attempts), possibly interleaved with gateway timeouts that never
        // reached the API — so the journey could not observe its product
        // claim: the run was neither observable as complete nor provably
        // wedged. Failing here would block an already-successful deploy on an
        // infrastructure outage; skip loudly instead so the outage is visible
        // without misreporting it as a product regression. A run that stays
        // incomplete while the API IS reachable (a re-issue returned a
        // deterministic 4xx/500) is NOT infra-blocked and still fails below.
        test.skip(true, `staging transient-outage storm left the HITL gate unobservable (run ${run.run_id}): ${outcome.lastError}`)
      }
      if (outcome.kind !== 'complete') {
        throw new Error(
          `run ${run.run_id} did not complete after re-issuing the approve decision; ` +
            `last poll: ${outcome.lastError}`,
        )
      }
      expect(outcome.status).toBe('complete')

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
