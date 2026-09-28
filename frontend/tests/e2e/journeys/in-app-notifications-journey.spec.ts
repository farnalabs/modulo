import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  cleanupJourneyEntities,
  createManualNodePipeline,
  pollRunStatus,
  triggerRun,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'

/**
 * Real-stack in-app notification journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. A run
 * parking at its HITL gate raises a real in-app notification (category
 * hitl.awaiting, org scope) through the notifier's event mapper; the dashboard
 * bell panel renders it with a link to the run, and dismissing it removes it
 * from the caller's view on the backend.
 *
 * Dismissal is self-scoped, so nothing is left behind for other users of the
 * shared instance; the pipeline and schema are deleted in a finally block.
 */

interface NotificationItem {
  id: string
  category: string
  title: string
  action_url: string | null
  run_id: string | null
  run_status: string | null
}

const isParked = (status: string) => status === 'awaiting_human' || status === 'hitl_parked'

/** Poll the real notifications list until OUR run's awaiting notification appears. */
async function waitForAwaitingNotification(apiBase: string, token: string, runId: string, timeoutMs = 60_000): Promise<NotificationItem> {
  const deadline = Date.now() + timeoutMs
  let lastError = 'no attempts completed'
  while (Date.now() < deadline) {
    const res = await apiFetch<{ items: NotificationItem[] }>(
      apiBase,
      token,
      'GET',
      '/api/v1/notifications/in-app?category=hitl.awaiting&page_size=50',
    )
    if (res.status === 200) {
      const found = res.body?.items.find((n) => n.run_id === runId)
      if (found) return found
      lastError = `category list returned ${res.body?.items.length ?? 0} rows, none linked to the run`
    } else {
      lastError = `GET /api/v1/notifications/in-app -> ${res.status}`
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000))
  }
  throw new Error(`[realstack] the hitl.awaiting notification never appeared within ${timeoutMs}ms (${lastError})`)
}

/** Best-effort cleanup: dismiss the notification for self if it still exists. */
async function dismissNotificationBestEffort(apiBase: string, token: string, notificationId: string | null): Promise<void> {
  if (!notificationId) return
  try {
    await apiFetch(apiBase, token, 'POST', `/api/v1/notifications/in-app/${notificationId}/dismiss`, {
      dismiss_scope: 'self',
    })
  } catch (err) {
    console.warn('[realstack] cleanup: notification dismiss failed:', err instanceof Error ? err.message : String(err))
  }
}

test.describe('Real-stack journeys: in-app notifications', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('a parked run raises a real HITL notification the bell panel shows and dismisses', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Notify'),
      uniqueName('E2E Notify Output Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    let notificationId: string | null = null
    try {
      const run = await triggerRun(apiBase, token, created.pipeline.id, { prompt: 'E2E journey notification run' })
      await pollRunStatus(apiBase, token, run.run_id, isParked, { timeoutMs: 120_000 })

      // The run parked; the notifier created a real in-app notification row
      // scoped to the org. Resolve it through the real API first so the panel
      // assertion below is deterministic.
      const notification = await waitForAwaitingNotification(apiBase, token, run.run_id)
      notificationId = notification.id
      expect(notification.category).toBe('hitl.awaiting')
      expect(notification.title).toContain(created.pipeline.name)
      expect(notification.action_url).toBe(`/runs/${run.run_id}`)
      expect(notification.run_id).toBe(run.run_id)

      // Rendered: the dashboard bell panel lists the notification with the
      // point-in-time run state (FAR-1234) and a link to the run.
      await loginAsAdmin(page, env)
      await page.goto('/')
      await page.getByTestId('notifications-panel-toggle').click()
      const card = page.locator('.notification-card').filter({ hasText: created.pipeline.name }).first()
      await expect(card).toBeVisible({ timeout: 30_000 })
      await expect(card.getByTestId('notification-run-state')).toContainText(/awaiting/i)
      await expect(card.getByRole('link', { name: /view run/i })).toHaveAttribute('href', `/runs/${run.run_id}`)

      // Dismiss for self through the dialog; the card leaves the panel.
      // NotificationCard keeps its action controls out of the layout until the
      // card is hovered (`.notification-actions` is display:none outside
      // :hover/:focus-within), and getByRole does not match a display:none
      // element — hover the card first so the control enters the a11y tree.
      await card.hover()
      await card.getByRole('button', { name: /dismiss this notification/i }).click()
      await page.getByRole('button', { name: 'Dismiss', exact: true }).click()
      await expect(card).toHaveCount(0, { timeout: 30_000 })

      // Persisted: the caller's dashboard no longer reports it.
      const dashRes = await apiFetch<{ notifications: NotificationItem[] }>(apiBase, token, 'GET', '/api/v1/notifications/in-app/dashboard')
      expect(dashRes.status).toBe(200)
      expect(dashRes.body?.notifications.some((n) => n.id === notification.id)).toBe(false)
      notificationId = null
    } finally {
      await dismissNotificationBestEffort(apiBase, token, notificationId)
      await cleanupJourneyEntities(cleanup)
    }
  })
})
