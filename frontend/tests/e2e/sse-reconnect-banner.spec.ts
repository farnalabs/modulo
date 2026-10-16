import { test, expect, loginAsAdmin } from './setup/fixtures'
import type { Page } from '@playwright/test'

/**
 * FAR-1842: the SSE "Live updates stopped" reconnect banner, end to end.
 *
 * The classification fix itself (401/403 terminal vs 429/other retry) is unit
 * covered in src/__tests__/useEventStream.test.ts; this spec locks the
 * browser-observable lifecycle the ticket's acceptance criteria ask for:
 *
 *  1. a terminal 401 (token refresh rejected) raises the banner;
 *  2. Reconnect acknowledges the click with in-flight feedback, shows the
 *     failure copy when the restart lands back on a terminal stop, and
 *     CLEARS when the next /api/v1/events response is a healthy 200 stream;
 *  3. a 429 never raises the terminal banner — the stream retries with backoff.
 *
 * Each stream outcome is forced with page.route so the terminal states are
 * reached deterministically on every target, without touching the real
 * session (the refresh endpoint is pinned to 401 so no token rotation can
 * quietly rescue the 401 case).
 */

type EventsMode = 'auth-fail' | 'forbidden' | 'rate-limited' | 'stream'

interface EventsHarness {
  /** How many requests the page has issued to /api/v1/events so far. */
  readonly requestCount: () => number
  /** What subsequent /api/v1/events responses return. */
  readonly setMode: (mode: EventsMode) => void
  /** Hold every /api/v1/events response from now until release(). */
  readonly hold: () => void
  /** Let held (and future) /api/v1/events responses through. */
  readonly release: () => void
}

/**
 * Install the routes that force each stream outcome. MUST be called AFTER
 * loginAsAdmin: Playwright matches routes in reverse registration order, so
 * these only take precedence over the local mock API's catch-all route when
 * registered later. (Never spell that catch-all glob inside a block comment —
 * the slash in a two-star glob closes the comment.)
 *
 * The bell's own REST needs (list / preferences / unread-count) are pinned to
 * empty-but-valid payloads so the assertions below never depend on backend
 * data. The list glob is registered FIRST because it matches every
 * `/notifications/in-app*` path — the narrower routes registered after it win
 * for their own URLs.
 */
async function installEventStreamRoutes(page: Page, initialMode: EventsMode): Promise<EventsHarness> {
  let mode = initialMode
  let held: Promise<void> | null = null
  let releaseHeld: (() => void) | null = null
  let requestCount = 0

  await page.route('**/api/v1/notifications/in-app*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 }),
    }),
  )
  await page.route('**/api/v1/notifications/in-app/preferences*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ dashboard_level: 'warning', notification_opt_outs: {} }),
    }),
  )
  await page.route('**/api/v1/notifications/in-app/unread-count*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ count: 0 }) }),
  )
  // A rejected refresh keeps the session dead: no fresh token, no second
  // fetch — the 401 reaches classifyHttpFailure() exactly as issued.
  await page.route('**/api/v1/auth/refresh', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Refresh token rejected' }),
    }),
  )
  await page.route('**/api/v1/events*', async (route) => {
    requestCount++
    if (held) await held
    if (mode === 'stream') {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: ': connected\n\n',
      })
    }
    if (mode === 'rate-limited') {
      return route.fulfill({ status: 429, contentType: 'text/plain', body: 'Too Many Requests' })
    }
    if (mode === 'forbidden') {
      return route.fulfill({ status: 403, contentType: 'text/plain', body: 'Forbidden' })
    }
    return route.fulfill({ status: 401, contentType: 'text/plain', body: 'Unauthorized' })
  })

  return {
    requestCount: () => requestCount,
    setMode: (next) => {
      mode = next
    },
    hold: () => {
      held = new Promise((resolve) => {
        releaseHeld = resolve
      })
    },
    release: () => {
      releaseHeld?.()
      releaseHeld = null
      held = null
    },
  }
}

test.describe('SSE reconnect banner lifecycle', { tag: '@regression' }, () => {
  test('terminal 401 raises the banner; a manual reconnect clears it on a healthy stream', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    const events = await installEventStreamRoutes(page, 'auth-fail')

    await page.goto('/notifications')

    // 1 — Appears: the stream stops terminally on 401 with a rejected refresh.
    const banner = page.getByTestId('sse-reconnect-banner')
    const button = banner.getByTestId('sse-reconnect-button')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText('Live updates stopped')
    await expect(button).toBeEnabled()

    // 2 — Reconnect gives feedback: the held restart keeps the in-flight copy
    //     on screen (button disabled, default copy replaced) while it runs.
    events.hold()
    await button.click()
    await expect(banner).toContainText('Reconnecting')
    await expect(banner).not.toContainText('Live updates stopped')
    await expect(button).toBeDisabled()

    // 3 — ...and when it lands back on a terminal stop, it says so instead of
    //     silently swallowing the failure.
    events.release()
    await expect(banner).toContainText('Reconnect failed')
    await expect(button).toBeEnabled()

    // 4 — Clears: a restart that reaches a healthy 200 stream removes the
    //     banner entirely (state derived, not dismissed).
    events.setMode('stream')
    events.hold()
    await button.click()
    await expect(banner).toContainText('Reconnecting')
    events.release()
    await expect(banner).toHaveCount(0)
  })

  test('a 429 never raises the terminal banner — the stream retries with backoff', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    const events = await installEventStreamRoutes(page, 'rate-limited')

    await page.goto('/notifications')

    // The stream must keep retrying after a 429 (first backoff retry lands
    // within ~1 s): if 429 were misclassified as terminal, requestCount would
    // stall at 1 and this poll would time out.
    await expect
      .poll(() => events.requestCount(), { timeout: 15_000 })
      .toBeGreaterThanOrEqual(2)

    // And the terminal banner must never have appeared — a rate limit is a
    // retry-classified condition, not a fresh-sign-in stop.
    await expect(page.getByTestId('sse-reconnect-banner')).toHaveCount(0)
  })

  test('a 403 permission rejection raises the terminal banner', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await installEventStreamRoutes(page, 'forbidden')

    await page.goto('/notifications')

    await expect(page.getByTestId('sse-reconnect-banner')).toBeVisible()
    await expect(page.getByTestId('sse-reconnect-banner')).toContainText('Live updates stopped')
  })
})
