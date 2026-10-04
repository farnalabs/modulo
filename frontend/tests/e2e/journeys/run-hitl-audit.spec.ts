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
    // The recovery wait below is bounded by a 90 s deadline, but that budget
    // is spent inside the shared test/beforeEach-hook timeout and the test
    // timeout BEGINS at hook start (Playwright: the test timeout is shared
    // with beforeEach) — Playwright's default 180 s can be consumed by the
    // hook alone before any in-test deadline is honoured. Size the timeout as
    // the sum of the budgets that can legitimately stack, plus margin, so
    // deadline + teardown always fit:
    //
    //   hook/login + pipeline setup         ~60 s
    //   park poll (bounded)                120 s
    //   UI: gate card / claim / approve     45 s
    //   bounded recovery + boundary observe 95 s
    //   post-recovery UI/list/audit         75 s
    //   best-effort cleanup                 30 s
    //   margin                              25 s
    //                                     ------
    //                                     450 s
    //
    // This is the same formula #1208 used for its 540 s (540 = 450 + the
    // extra 90 s it added to the recovery window). That window existed only
    // to ride out the "staging 503 storm", which was then proven to be a
    // deterministic approve-path product defect (FAR-1408/#1230), not an
    // outage — no window rides out a defect that reproduces on every request.
    // The window is back to #1183's 90 s, which covers a genuine transient
    // blip (each helper request already retries an explicit 502/503/504 up to
    // 4x20 s with linear backoff before this loop is even needed), so the
    // timeout comes back down by the same 90 s.
    test.setTimeout(450_000)
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

      // The run must really resume and complete on the backend. A GENUINE
      // transient failure of the approve request (an explicit 502/503/504 from
      // a staging DB/connection blip, which rolls back and is safe to
      // re-issue) is tolerated: every helper request already retries that
      // status with bounded linear backoff (apiFetch), and if the run is still
      // short of a decision the loop below re-issues the approve through the
      // real API until its deadline. Recovery MUST bypass the UI — the first
      // approve keeps the control disabled while in flight, and once the
      // re-claim has flipped the run to `claimed` the UI claim is refused —
      // and the loop's own re-issue, NOT the committed-decision reconcile
      // (which skips a claimed-but-undecided row unconditionally), is what
      // carries such a run to completion. The loop and its rationale live in
      // waitForRunCompletionWithHitlRecovery; it is deadline-bounded (never a
      // fixed iteration count, whose worst case overran the hook timeout).
      //
      // A run that has NOT completed when that window closes FAILS this test.
      // That deliberately reverses #1214's skip: the "sustained staging 503
      // storm" the skip was written for was not an infrastructure outage but a
      // deterministic product defect in the endpoint itself — `approve_review`
      // queried `_validate_choice_answer` outside any transaction on a DI
      // session built with `autobegin=False`, raising
      // `sqlalchemy.exc.InvalidRequestError` (a `SQLAlchemyError` subclass)
      // that the error map reported as 503 "Database temporarily unavailable."
      // on EVERY approve request, reproduced 3/3 with no database fault and
      // fixed in #1230 (FAR-1408). The signature was visible in the failures
      // themselves: status polls kept returning 200 `claimed` while approve
      // 503'd, so the API and DB were reachable the whole time. Skipping on
      // that signature let this journey pass without ever observing its own
      // claim — the one thing a regression gate must never do.
      const outcome = await waitForRunCompletionWithHitlRecovery(apiBase, token, run.run_id, {
        deadlineMs: 90_000,
        notes: 'E2E journey approval',
      })
      if (outcome.kind !== 'complete') {
        throw new Error(
          `run ${run.run_id} did not complete after re-issuing the approve decision within the ` +
            `bounded recovery window (a persistent approve 5xx is a product failure, not an outage): ` +
            `${outcome.lastError}`,
        )
      }

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
