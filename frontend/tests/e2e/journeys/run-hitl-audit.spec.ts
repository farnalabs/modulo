import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  cleanupJourneyEntities,
  createPipeline,
  createSchemaWithVersion,
  pollRunStatus,
  setPipelineGraph,
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

interface ManualNodePipeline {
  pipeline: { id: string; name: string }
  schemaId: string
  nodeId: string
}

/**
 * Create a pipeline whose graph is a single manual-input node bound to a
 * fresh output schema (the backend requires manual nodes to declare an
 * output schema and a label). A manual node never calls an LLM: it interrupts
 * the run until a human supplies a decision.
 */
async function createManualNodePipeline(
  apiBase: string,
  token: string,
  pipelineName: string,
  schemaName: string,
): Promise<ManualNodePipeline> {
  const schemaId = await createSchemaWithVersion(apiBase, token, schemaName)
  const pipeline = await createPipeline(apiBase, token, pipelineName)
  const nodeId = crypto.randomUUID()
  await setPipelineGraph(apiBase, token, pipeline.id, [
    {
      id: nodeId,
      node_type: 'manual',
      label: 'E2E Human Input',
      position: { x: 120, y: 120 },
      output_schema_id: schemaId,
    },
  ])
  return { pipeline, schemaId, nodeId }
}

const isParked = (status: string) => status === 'awaiting_human' || status === 'hitl_parked'

/**
 * Resolve a run id through the REAL API (never through the UI-triggered
 * page URL): list the pipeline's newest run (the endpoint orders by
 * created_at desc by default). Used by the editor-triggered journey because
 * the editor's own post-run navigation is broken upstream —
 * PipelineEditorView.triggerRun reads `(data as any).id` while POST
 * /api/v1/runs returns `run_id`, so the UI lands on /runs/undefined. The
 * trigger itself still works (a real run is created); pinning the parked
 * state to a direct /runs/{id} navigation keeps the journey deterministic.
 * Upstream bug tracked in farnalabs/modulo#1014 — once it is fixed this can
 * pin waitForURL('/runs/{id}') instead and the workaround be removed.
 */
async function resolveLatestRunId(apiBase: string, token: string, pipelineId: string): Promise<string> {
  const deadline = Date.now() + 30_000
  let lastErr = 'no attempt completed'
  while (Date.now() < deadline) {
    const res = await apiFetch<{ items: Array<Record<string, unknown>> }>(
      apiBase,
      token,
      'GET',
      `/api/v1/runs?pipeline_id=${pipelineId}&page_size=1`,
    )
    const item = res.status === 200 ? res.body?.items?.[0] : undefined
    if (item?.run_id) return String(item.run_id)
    lastErr = `GET /api/v1/runs -> ${res.status}, items: ${res.body?.items?.length ?? 0}`
    await new Promise((resolve) => setTimeout(resolve, 2_000))
  }
  throw new Error(
    `[realstack] the editor-triggered run never appeared in GET /api/v1/runs within 30s (${lastErr})`,
  )
}

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
      // triggers the run through POST /api/v1/runs).
      await page.goto(`/pipelines/${created.pipeline.id}/editor`)
      await expect(page.getByTestId('pipeline-editor-run')).toBeEnabled()
      await page.getByTestId('pipeline-editor-run').click()
      await page.getByTestId('pipeline-editor-run-prompt').fill('E2E journey run prompt')
      await page.getByTestId('pipeline-editor-run-submit').click()

      // The run id comes from the REAL API (the newest run for the pipeline),
      // not from the UI — the editor's post-trigger navigation is broken
      // upstream (see resolveLatestRunId doc comment) and a UI-URL pin would
      // follow a /runs/undefined dead end instead of the run.
      const runId = await resolveLatestRunId(apiBase, token, created.pipeline.id)

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

      // Observable effect: the decision is hoisted as success feedback...
      await expect(page.getByTestId('run-detail-hitl-message')).toBeVisible({ timeout: 15_000 })
      // ...and the run really resumed and completed on the backend.
      const status = await pollRunStatus(apiBase, token, run.run_id, (s) => s === 'complete', { timeoutMs: 120_000 })
      expect(status).toBe('complete')

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
