import { test, expect, loginAsAdmin } from './setup/fixtures'

function makeNotifications(count: number, startId = 1) {
  return Array.from({ length: count }, (_, i) => ({
    id: `n${startId + i}`,
    scope: 'org',
    level: 'info',
    category: 'pipeline_run',
    title: `Notification ${startId + i}`,
    body: `Body ${startId + i}`,
    action_url: null,
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    created_at: '2025-06-01T10:00:00Z',
    scope_label: 'Organization',
  }))
}

test.describe('Notifications', { tag: "@regression" }, () => {
  test('notifications page loads', { tag: "@regression" }, async ({ page, env }) => {
    await page.route('**/api/v1/notifications/in-app*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/notifications')

    await expect(page.locator('h1')).toContainText(/Notification/i)
  })

  test('notification list renders elements', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/notifications/in-app*', (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.includes('/dashboard')) { route.fallback(); return }
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'n1',
              scope: 'org',
              level: 'success',
              category: 'pipeline_run',
              title: 'Pipeline run completed',
              body: 'My Pipeline finished successfully',
              action_url: null,
              dismiss_strategy: 'user_only',
              dismissible_at_scope: false,
              created_at: '2025-06-01T10:00:00Z',
              scope_label: 'Organization',
            },
            {
              id: 'n2',
              scope: 'org',
              level: 'warning',
              category: 'hitl_review',
              title: 'HITL review requested',
              body: 'Approval needed for production deploy',
              action_url: null,
              dismiss_strategy: 'user_only',
              dismissible_at_scope: false,
              created_at: '2025-06-01T11:00:00Z',
              scope_label: 'Organization',
            },
          ],
          total: 2,
          page: 1,
          page_size: 20,
        }),
      })
    })

    await page.goto('/notifications')

    await expect(page.locator('text=Pipeline run completed')).toBeVisible()
    await expect(page.locator('text=HITL review requested')).toBeVisible()
  })

  test('notifications page defaults to status=active filter', { tag: "@regression" }, async ({ page, env }) => {
    let receivedParams: Record<string, string> = {}
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/notifications/in-app?**', (route) => {
      const url = new URL(route.request().url())
      for (const [k, v] of url.searchParams.entries()) {
        receivedParams[k] = v
      }
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }),
      })
    })

    await page.goto('/notifications')

    // FAR-250: the app shell opens a long-lived SSE stream to /api/v1/events,
    // so `networkidle` never resolves. Wait for the notifications request
    // itself instead of network quiescence.
    await expect
      .poll(() => receivedParams['status'], {
        message: 'Notifications list request must include a status filter',
      })
      .toBe('active')
  })

  test('notifications page auto-applies filters without Apply button', { tag: "@regression" }, async ({ page, env }) => {
    const requests: string[] = []
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/notifications/in-app?**', (route) => {
      const url = new URL(route.request().url())
      requests.push(url.searchParams.toString())
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }),
      })
    })

    await page.goto('/notifications')

    // FAR-250: the app shell opens a long-lived SSE stream to /api/v1/events,
    // so `networkidle` never resolves. Wait for the list request itself.
    await expect
      .poll(() => requests.length, {
        message: 'Notifications list must be requested on load',
      })
      .toBeGreaterThan(0)

    // The Apply Filters button should not exist
    await expect(page.locator('[data-testid="notifications-apply-filters"]')).toHaveCount(0)
  })

  test('dashboard panel shows paging controls for 10+ notifications', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/notifications/in-app?**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: makeNotifications(10),
          total: 15,
          page: 1,
          page_size: 10,
        }),
      })
    })
    await page.route('**/api/v1/notifications/in-app/dashboard', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ notifications: makeNotifications(5), total_unread: 15 }),
      })
    })

    await page.goto('/')
    // Expand the notifications panel
    const toggle = page.locator('[data-testid="notifications-panel-toggle"]')
    await toggle.click()

    // Should show page info
    await expect(page.locator('text=/Page \\d+ of \\d+/')).toBeVisible()
    // Should show next button
    await expect(page.locator('[data-testid="panel-next-page"]')).toBeVisible()
  })

  // FAR-1234 — a stale HITL request for a stopped run must not read as live,
  // and must be clearable end to end.
  test('stale HITL notification for a cancelled run shows run state and can be dismissed', { tag: "@regression" }, async ({ page, env }) => {
    const runId = '2fdb083f-518a-4e71-8a5d-ffb573602f7a'
    let dismissed = false

    const staleHitl = {
      id: 'n-stale',
      scope: 'org',
      level: 'info',
      category: 'hitl.awaiting',
      title: 'HITL review needed — Improve Security',
      body: 'Pipeline "Improve Security" is waiting for human review.',
      action_url: `/runs/${runId}`,
      dismiss_strategy: 'any_scope',
      dismissible_at_scope: true,
      created_at: '2026-09-25T13:02:50Z',
      expires_at: '2026-09-28T13:02:50Z',
      scope_label: 'Org-wide',
      run_id: runId,
      run_status: 'cancelled',
      run_terminal: true,
      run_cancel_reason: null,
    }

    const listPayload = () =>
      dismissed
        ? { items: [], total: 0, page: 1, page_size: 20 }
        : { items: [staleHitl], total: 1, page: 1, page_size: 20 }

    await loginAsAdmin(page, env)
    // Registered AFTER the local mock API so this handler wins the precedence
    // contest (Playwright matches the most recently registered route first).
    await page.route('**/api/v1/notifications/in-app*', (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/dismiss')) {
        dismissed = true
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'dismissed_for_self' }),
        })
        return
      }
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(listPayload()) })
    })

    await page.goto('/notifications')

    const card = page.locator('.notification-card').first()
    // Point-in-time metadata: the run's CURRENT state, not the trigger-time claim.
    await expect(card.getByTestId('notification-run-state')).toContainText('cancelled')
    // The HITL request is demoted: never presented as a pending review.
    await expect(card).toContainText('No review needed')
    await expect(card).not.toContainText('Awaiting your review')

    // Dismiss it end to end (the controls reveal on hover on a pointer device).
    await card.hover()
    await card.getByRole('button', { name: 'Dismiss this notification' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Dismiss', exact: true }).click()

    await expect(page.locator('.notification-card')).toHaveCount(0)
    // An emptied inbox renders the empty state (the count/pagination row only
    // exists while there is at least one item).
    await expect(page.getByText('No notifications', { exact: true })).toBeVisible()
  })
})
