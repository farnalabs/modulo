import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiLogin,
  createPipeline,
  createTrigger,
  deletePipelineBestEffort,
  deleteTrigger,
  listTriggers,
  uniqueName,
} from '../setup/realstack-api'

/**
 * Real-stack trigger journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target, where
 * the whole API is page.route-mocked by setupLocalMockApi. settings-triggers.spec.ts
 * page.route-mocks the very trigger list it claims to verify; this journey
 * creates a real cron trigger and drives the pause toggle the operator would
 * use, asserting the persisted state through the backend.
 *
 * The trigger can essentially never fire a run (its only slot is Sunday
 * 03:30 UTC and the test pauses it seconds after creation), the pipeline has
 * no nodes so a stray fire is refused rather than executed, and the trigger
 * is deleted inside the test body so the shared instance keeps no residue;
 * the finally block only covers the failure paths.
 */

test.describe('Real-stack journeys: trigger lifecycle', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating a cron trigger persists it with a resolved next-fire time, and pausing it persists inactive', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const pipelineName = uniqueName('E2E Journey Trigger Pipeline')
    const pipeline = await createPipeline(apiBase, token, pipelineName)
    let triggerId: string | null = null
    let deletedThroughApi = false
    try {
      const trigger = await createTrigger(apiBase, token, pipeline.id, {
        trigger_type: 'cron',
        cron_expression: '30 3 * * 0',
        cron_timezone: 'UTC',
        active: true,
      })
      triggerId = trigger.id

      // Observable effect: the backend computed a real next-fire time for the
      // expression at creation (the cron-resolution seam).
      expect(trigger.next_fire_at).toBeTruthy()

      await loginAsAdmin(page, env)
      await page.goto('/settings/triggers')

      // The persisted trigger renders in the table: scoped to OUR pipeline
      // (row text) with its resolved type.
      const row = page.locator('tr').filter({ hasText: pipelineName }).first()
      await expect(row).toBeVisible({ timeout: 30_000 })
      await expect(row).toContainText(/cron/i)
      await expect(row.getByTestId('settings-triggers-toggle')).toContainText('Active')

      // Operator seam: pause it from the table.
      await row.getByTestId('settings-triggers-toggle').click()
      await expect(row.getByTestId('settings-triggers-toggle')).toContainText('Inactive', { timeout: 30_000 })

      // Persisted: the backend reports the paused state.
      const paused = await listTriggers(apiBase, token, pipeline.id)
      const pausedRow = paused.items.find((t) => t.id === trigger.id)
      expect(pausedRow, 'paused trigger must still be listed for its pipeline').toBeTruthy()
      expect(pausedRow?.active).toBe(false)

      // Toggle it back on so the delete path starts from the created state.
      await row.getByTestId('settings-triggers-toggle').click()
      await expect(row.getByTestId('settings-triggers-toggle')).toContainText('Active', { timeout: 30_000 })
      const resumed = await listTriggers(apiBase, token, pipeline.id)
      expect(resumed.items.find((t) => t.id === trigger.id)?.active).toBe(true)

      // Delete through the real API, then prove it is really gone
      // (soft-deleted triggers are excluded from the list).
      await deleteTrigger(apiBase, token, trigger.id)
      deletedThroughApi = true
      const remaining = await listTriggers(apiBase, token, pipeline.id)
      expect(remaining.items.some((t) => t.id === trigger.id)).toBe(false)
    } finally {
      if (triggerId && !deletedThroughApi) {
        try {
          await deleteTrigger(apiBase, token, triggerId)
        } catch (err) {
          console.warn('[realstack] cleanup: trigger delete failed:', err instanceof Error ? err.message : String(err))
        }
      }
      await deletePipelineBestEffort(apiBase, token, pipeline.id)
    }
  })
})
