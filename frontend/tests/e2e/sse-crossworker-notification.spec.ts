import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * FAR-250: an already-open page receives a notification over the SSE stream
 * and updates its badge WITHOUT a reload.
 *
 * The full cross-worker path (notifier -> Redis -> web relay -> SSE) is
 * covered by the backend fakeredis relay integration; this spec locks the
 * observable browser behaviour on top of it: the shared fetch-based stream
 * delivers `resource_changed` for a notification, the bell's handler
 * refetches the unread count, and read-time suppression (client prefs) is
 * wired before the first event.
 */
test.describe('SSE notification delivery', { tag: '@regression' }, () => {
  test('an open page updates the notification badge from an SSE event without reloading', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    // Stateful unread count: 1 until the SSE event "creates" a notification.
    let unreadCount = 1
    await page.route('**/api/v1/notifications/in-app/unread-count*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ count: unreadCount }),
      }),
    )
    // Client prefs load BEFORE the stream connects (read-time suppression).
    await page.route('**/api/v1/notifications/in-app/preferences*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ dashboard_level: 'warning', notification_opt_outs: {} }),
      }),
    )
    // Notifications list for the page itself.
    await page.route('**/api/v1/notifications/in-app?*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 }),
      }),
    )
    // SSE stream: connected frame + one notification event, then close.
    const notificationEvent = {
      type: 'notification',
      id: 'e2e-notification-1',
      action: 'created',
      version: 1,
      org_id: 'org-1',
      notification_id: 'e2e-notification-1',
      category: 'run_failed',
      created_at: '2026-09-23T12:00:00+00:00',
      event_id: 'notification:e2e-notification-1:created',
    }
    await page.route('**/api/v1/events*', (route) => {
      // The notification now exists server-side; the handler's refetch sees it.
      unreadCount = 2
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body:
          `: connected\n\n` +
          `event: resource_changed\n` +
          `data: ${JSON.stringify(notificationEvent)}\n\n`,
      })
    })

    await page.goto('/notifications')

    // Initial REST fetch renders the starting badge...
    await expect(page.getByTestId('notification-unread-badge')).toHaveText('1')

    // ...the SSE event arrives on the already-open page and the badge updates
    // with NO navigation/reload.
    await expect(page.getByTestId('notification-unread-badge')).toHaveText('2')
  })
})
