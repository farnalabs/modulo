import type { Locator, Page } from '@playwright/test'
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
    //
    // The trailing glob must be `**`, not `*`: a single `*` matches any
    // characters EXCEPT `/`, so `.../in-app*` matched the list GET
    // (`.../in-app?page=1...`) but not the dismiss POST
    // (`.../in-app/<id>/dismiss`). The POST then escaped the mock, hit the real
    // target, and 422'd ("path.notification_id: Input should be a valid UUID")
    // because the fixture id is not a UUID. The card consequently never
    // dismissed on staging while the local mock quantified the request and hid
    // the bug.
    await page.route('**/api/v1/notifications/in-app**', (route) => {
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

// FAR-1926 — viewport-anchored bell dropdown.
//
// The dropdown was `position: absolute; right: 0; width: 20rem` inside the
// bell's own ~56px-wide root. Anchored to a bell that sits at the far LEFT of
// the screen (the mobile icon rail and the desktop collapsed rail) the 320px
// panel extended off the LEFT edge of the viewport, and the host chrome's
// `overflow-hidden` clipped whatever remained. These tests measure the panel's
// real bounding box in every chrome that hosts the bell, and hit-test its
// centre so a geometry that is "inside the viewport" on paper but clipped by
// an ancestor still fails.
test.describe('Notification bell dropdown viewport anchoring (FAR-1926)', { tag: '@regression' }, () => {
  const NARROW = { width: 360, height: 740 } as const
  const DESKTOP = { width: 1280, height: 800 } as const

  const planPayload = (rail: boolean) => ({
    flags: [
      {
        name: 'mobile_sidebar_rail',
        description: 'Left icon rail on mobile',
        tier: 'team',
        currently_active: rail,
        depends_on: null,
      },
    ],
    // Must be a tier the TARGET's catalog actually lists. On staging the
    // catalog carries only community/team (no enterprise), so an unknown tier
    // leaves `tierRanks[currentTier]` undefined: the dashboard route's
    // required_tier (community) then fails `isAtMinimumTier`, the guard
    // self-redirects '/' → 'dashboard', and the SPA never mounts — the tests
    // below starve on the bootstrap splash. The local fixture catalog invents
    // an 'enterprise' rank, which is why an enterprise tier passes locally but
    // hangs against staging.
    license: { tier: 'team' },
    dev_mode: true,
  })

  const sampleNotification = {
    id: 'n-far1926',
    scope: 'org',
    level: 'info',
    category: 'pipeline_run',
    title: 'Viewport anchoring sample',
    body: 'A notification that makes the dropdown render real content.',
    action_url: null,
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    created_at: '2025-06-01T10:00:00Z',
    scope_label: 'Organization',
  }

  /**
   * Sign in on the local mock API, then register the chrome/notification
   * routes. Routes registered AFTER `loginAsAdmin`'s catch-all take precedence
   * (Playwright matches the most recently registered handler first), so these
   * own the plan bootstrap (which selects the mobile chrome) and the bell's
   * feed.
   */
  async function bootWithChrome(page: Page, env: Parameters<typeof loginAsAdmin>[1], opts: { desktop?: boolean; rail: boolean }) {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/plan', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(planPayload(opts.rail)),
      }),
    )
    await page.route('**/api/v1/notifications/in-app/dashboard*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ notifications: [sampleNotification], total_unread: 1 }),
      }),
    )
    await page.route('**/api/v1/notifications/in-app/unread-count*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ count: 1 }),
      }),
    )
    await page.setViewportSize(opts.desktop ? DESKTOP : NARROW)
    await page.goto('/')
  }

  async function openBell(page: Page): Promise<{ dropdown: Locator; box: NonNullable<Awaited<ReturnType<Locator['boundingBox']>>> }> {
    await page.locator('[data-testid="notification-bell-button"]').click()
    const dropdown = page.locator('[data-testid="notification-dropdown"]')
    await expect(dropdown).toBeVisible()
    const box = await dropdown.boundingBox()
    expect(box, 'the open dropdown must have a bounding box').not.toBeNull()
    return { dropdown, box: box! }
  }

  function expectWithinViewport(box: { x: number; width: number }, viewportWidth: number) {
    expect(box.x, `dropdown left edge (${box.x}px) must not be off-screen`).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width, `dropdown right edge (${box.x + box.width}px) must not exceed the ${viewportWidth}px viewport`)
      .toBeLessThanOrEqual(viewportWidth)
    // A 50px sliver clipped by an ancestor still sits "within" the viewport —
    // require a real panel (the 320px design width, bounded on narrow screens).
    expect(box.width, 'dropdown panel width').toBeGreaterThanOrEqual(280)
  }

  /** Hit-test the panel's upper-centre: a clipped region is not the topmost target. */
  async function expectNotClipped(page: Page, dropdown: Locator) {
    const box = await dropdown.boundingBox()
    expect(box, 'the open dropdown must have a bounding box').not.toBeNull()
    const point: [number, number] = [
      Math.round(box!.x + box!.width / 2),
      Math.round(box!.y + Math.min(box!.height / 2, 48)),
    ]
    const hit = await page.evaluate(([x, y]) => {
      const el = document.elementFromPoint(x, y)
      return Boolean(el && el.closest('[data-testid="notification-dropdown"]'))
    }, point)
    expect(hit, `the dropdown centre (${point[0]},${point[1]}) must be the topmost hit target — not clipped by host chrome`).toBe(true)
  }

  test('mobile icon rail: the dropdown stays inside a 360px viewport', { tag: '@regression' }, async ({ page, env }) => {
    // Cold Vite dev compile on first navigation can blow the 30s local budget.
    test.setTimeout(90_000)
    await bootWithChrome(page, env, { rail: true })
    // Flag-ON mobile chrome: the 64px icon rail (bell at the far LEFT edge).
    await expect(page.locator('[aria-label="Expand sidebar"]')).toBeVisible()

    const { dropdown, box } = await openBell(page)
    expectWithinViewport(box, NARROW.width)
    await expectNotClipped(page, dropdown)
  })

  test('mobile drawer header: the dropdown stays inside a 360px viewport', { tag: '@regression' }, async ({ page, env }) => {
    // Cold Vite dev compile on first navigation can blow the 30s local budget.
    test.setTimeout(90_000)
    await bootWithChrome(page, env, { rail: false })
    // Flag-OFF mobile chrome: the hamburger header + slide-in drawer.
    await expect(page.locator('[aria-controls="mobile-sidebar"]')).toBeVisible()

    const { dropdown, box } = await openBell(page)
    expectWithinViewport(box, NARROW.width)
    await expectNotClipped(page, dropdown)
  })

  test('desktop: the dropdown stays inside the viewport in the expanded sidebar AND the collapsed rail', { tag: '@regression' }, async ({ page, env }) => {
    // Cold Vite dev compile on first navigation can blow the 30s local budget.
    test.setTimeout(90_000)
    await bootWithChrome(page, env, { desktop: true, rail: false })
    await expect(page.locator('[aria-label="Collapse sidebar"]')).toBeVisible()

    // Expanded sidebar: the dropdown must still open fully inside the window.
    const expanded = await openBell(page)
    expectWithinViewport(expanded.box, DESKTOP.width)
    await expectNotClipped(page, expanded.dropdown)

    // Collapse to the 64px rail and reopen — the historical off-screen case.
    await page.keyboard.press('Escape')
    await expect(expanded.dropdown).toHaveCount(0)
    await page.locator('[aria-label="Collapse sidebar"]').click()
    await expect(page.locator('[aria-label="Expand sidebar"]')).toBeVisible()

    const collapsed = await openBell(page)
    expectWithinViewport(collapsed.box, DESKTOP.width)
    await expectNotClipped(page, collapsed.dropdown)
  })
})
