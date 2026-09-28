import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  cancelRun,
  cleanupJourneyEntities,
  createManualNodePipeline,
  createTrigger,
  deleteTrigger,
  fireWebhook,
  getRun,
  listTriggers,
  pollRunStatus,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'

/**
 * Real-stack webhook delivery journey (FAR-1242 batch 3).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * webhook-boundary seam no other batch exercises: an external sender POSTs a
 * JSON body to the trigger's public delivery channel (naked HTTP, HMAC-less
 * triggers accept unauthenticated deliveries by design) and a REAL run is
 * created through the engine's advisory-locked delivery path — dedup, flood
 * protection and event acceptance included.
 *
 * The delivery is made ONCE with a unique payload (a re-send of the identical
 * body would 400 on byte-level dedup). The delivered event is verified in
 * two persisted places: the admin trigger-events API row and the trigger's
 * own last_fired_at timestamp.
 *
 * Cleanup: the parked run is cancelled (manual nodes never finish alone),
 * then trigger + pipeline + schema are deleted. All inside finally.
 */

test.describe('Real-stack journeys: webhook trigger delivery', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('a naked webhook delivery creates a real run on the HMAC-less trigger', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Webhook Pipeline'),
      uniqueName('E2E Webhook Output Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)

    let triggerId: string | null = null
    let runId: string | null = null
    try {
      const trigger = await createTrigger(apiBase, token, created.pipeline.id, {
        trigger_type: 'webhook',
        active: true,
      })
      triggerId = trigger.id

      // The delivery boundary: post a unique payload from OUTSIDE the app.
      const marker = uniqueName('e2e-webhook-payload-marker')
      const delivery = await fireWebhook(apiBase, trigger.id, { marker })
      // Observable effect: the backend ACCEPTED the delivery and created a
      // run — 202 with a real run id (not a queued/busy ack).
      expect(delivery.status).toBe(202)
      expect(delivery.body?.status).toBe('accepted')
      expect(delivery.body?.run_id).toBeTruthy()
      runId = delivery.body?.run_id ?? null
      if (!runId) throw new Error('[realstack] webhook delivery returned no run id')

      // Persisted: the run belongs to THIS trigger's pipeline and is parked
      // at the manual gate (a manual node waits for a human decision).
      const run = await getRun(apiBase, token, runId)
      expect(run.status).toBe(200)
      expect(run.body?.pipeline_id).toBe(created.pipeline.id)
      const parked = await pollRunStatus(
        apiBase,
        token,
        runId,
        (s) => s === 'awaiting_human' || s === 'hitl_parked',
        { timeoutMs: 90_000 },
      )
      expect(parked === 'awaiting_human' || parked === 'hitl_parked').toBe(true)

      // Trigger event log — persisted delivery record.
      const events = await apiFetch<{
        items: Array<{
          trigger_id: string
          trigger_type: string
          validation_result: string
          run_id: string | null
        }>
      }>(apiBase, token, 'GET', '/api/v1/admin/trigger-events?limit=50')
      expect(events.status).toBe(200)
      const accepted = events.body?.items.find(
        (e) => e.trigger_id === trigger.id && e.run_id === runId && e.validation_result === 'accepted',
      )
      expect(accepted, 'the delivered webhook must be recorded as an accepted trigger event').toBeTruthy()
      expect(accepted?.trigger_type).toBe('webhook')

      // The trigger row re-renders on /settings/triggers; the backend now
      // records a real last-fired time for the delivery.
      await loginAsAdmin(page, env)
      await page.goto('/settings/triggers')
      const row = page.locator('tr').filter({ hasText: created.pipeline.name }).first()
      await expect(row).toBeVisible({ timeout: 30_000 })
      await expect.poll(async () => {
        const listed = await listTriggers(apiBase, token, created.pipeline.id)
        return listed.items.find((t) => t.id === trigger.id)?.last_fired_at ?? null
      }, { timeout: 30_000, intervals: [1_000, 2_000, 5_000] }).toBeTruthy()

      // Cleanup order: cancel the parked run (manual nodes never finish on
      // their own) so the pipeline becomes deletable.
      await cancelRun(apiBase, token, runId)
      await pollRunStatus(apiBase, token, runId, (s) => s === 'cancelled', { timeoutMs: 90_000 })
      runId = null
      await deleteTrigger(apiBase, token, trigger.id)
      triggerId = null

      // The deleted trigger drops out of the pipeline scope for good.
      const remaining = await listTriggers(apiBase, token, created.pipeline.id)
      expect(remaining.items.some((t) => t.id === trigger.id)).toBe(false)
    } finally {
      if (triggerId) {
        try {
          await deleteTrigger(apiBase, token, triggerId)
        } catch (err) {
          console.warn('[realstack] cleanup: trigger delete failed:', err instanceof Error ? err.message : String(err))
        }
      }
      if (runId) {
        try {
          await cancelRun(apiBase, token, runId)
        } catch (err) {
          // Already cancelled/terminal — cleanup continues below.
          console.warn('[realstack] cleanup: run cancel failed:', err instanceof Error ? err.message : String(err))
        }
      }
      await cleanupJourneyEntities(cleanup)
    }
  })
})
