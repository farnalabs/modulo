import { test, expect, loginAsAdmin } from './setup/fixtures'

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
})
