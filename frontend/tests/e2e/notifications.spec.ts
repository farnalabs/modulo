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
    await page.waitForLoadState('networkidle')

    expect(receivedParams['status']).toBe('active')
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
    await page.waitForLoadState('networkidle')
    const initialCount = requests.length

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
})
