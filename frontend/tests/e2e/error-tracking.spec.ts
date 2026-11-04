import type { Page, Route } from '@playwright/test'
import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Error Tracking', { tag: "@regression" }, () => {
  test.beforeEach(async ({ page }) => {
    // The Assistant panel is a position:fixed overlay. Even though it now defaults
    // to 'closed' (FAR-869), a stale persisted 'docked'/'floating' state can
    // still render it over the filter bar and intercept the Reset click with
    // "subtree intercepts pointer events". Force it closed before the app boots
    // so it can never cover [data-testid=admin-errors-reset]. Mirrors
    // json-viewer.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('error dashboard page loads', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/errors')

    await expect(page.locator('h1')).toContainText(/Error/i, { timeout: 30_000 })
  })

  test('error dashboard shows UI elements', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/errors*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'e1',
              sample_message: 'Connection timeout',
              level_peak: 'error',
              count: 15,
              first_seen: '2025-06-01T12:00:00Z',
              last_seen: '2025-06-01T12:00:00Z',
              status: 'new',
            },
          ],
          total: 1,
        }),
      })
    })

    await page.goto('/admin/errors')

    await expect(page.locator('text=Connection timeout')).toBeVisible({ timeout: 30_000 })
  })

  test('auto-applies filters without Apply button (FAR-868)', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/errors')

    // The Apply Filters button must not exist
    const applyBtn = page.locator('button', { hasText: /apply filters/i })
    await expect(applyBtn).toHaveCount(0)

    // Reset button must exist
    await expect(page.getByTestId('admin-errors-reset')).toBeVisible({ timeout: 30_000 })

    // Changing a dropdown filter should auto-apply (no Apply button needed)
    const levelFilter = page.getByTestId('filter-bar-level')
    if (await levelFilter.isVisible()) {
      await levelFilter.click()
      const errorOption = page.locator('.p-select-option', { hasText: 'Error' })
      if (await errorOption.isVisible()) {
        await errorOption.click()
      }
    }

    // Reset should clear filters
    await page.getByTestId('admin-errors-reset').click()
    await expect(page.getByTestId('admin-errors-reset')).toBeVisible({ timeout: 30_000 })
  })
})

// ---------------------------------------------------------------------------
// FAR-2354: the two error-surface "Copy support reference" controls.
//
// The list row and the group detail each carry one copy action, built by the
// shared `lib/supportReference` helper. Both were verified by hand once and had
// no browser coverage, so a refactor could drop the full group id or the
// fingerprint and CI would stay green. These specs drive the real controls and
// read the real clipboard.
//
// `/admin/errors` is `required_roles: [admin]` in the manifest, so the
// read-only `/demo` principal can never reach it — these specs use the suite's
// authenticated admin session (`loginAsAdmin`) like the rest of this file.
// ---------------------------------------------------------------------------

/** A real 36-char uuid (8-4-4-4-12), so a shortened id cannot pass. */
const SUPPORT_GROUP_ID = 'd4e6f8a0-2b4c-4d6e-8f01-3a5c7e9b1d2f'
const SUPPORT_GROUP_FINGERPRINT = 'fp-9f2a7c1e4b6d8a03'
const SUPPORT_GROUP_LEVEL = 'error'
const SUPPORT_GROUP_COUNT = 42
const SUPPORT_GROUP_SOURCE = 'backend'

function groupSummary(): Record<string, unknown> {
  return {
    id: SUPPORT_GROUP_ID,
    fingerprint: SUPPORT_GROUP_FINGERPRINT,
    status: 'new',
    level_peak: SUPPORT_GROUP_LEVEL,
    count: SUPPORT_GROUP_COUNT,
    first_seen: '2026-08-01T10:00:00Z',
    last_seen: '2026-08-01T12:00:00Z',
    sample_message: 'Connection refused while dispatching node',
  }
}

function groupDetail(): Record<string, unknown> {
  return {
    ...groupSummary(),
    assigned_to: null,
    // Only the detail payload carries the reporting source; the list payload
    // does not, so the list reference omits the segment (never invents it).
    sample_event: {
      id: 'ev-1',
      level: SUPPORT_GROUP_LEVEL,
      message: 'Connection refused while dispatching node',
      stacktrace: null,
      context_json: null,
      source: SUPPORT_GROUP_SOURCE,
      environment: 'production',
      version: null,
      breadcrumbs: null,
      created_at: '2026-08-01T12:00:00Z',
    },
  }
}

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
}

/**
 * Mock every error-tracking read the two surfaces make. Registered AFTER
 * `loginAsAdmin` (which installs the catch-all mock), so these handlers win.
 * One glob plus a pathname dispatch — the list, the detail and the events
 * endpoint all live under `/api/v1/errors`.
 */
async function mockErrorsApi(page: Page): Promise<void> {
  await page.route('**/api/v1/errors**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/scheduler-starvation')) {
      return fulfillJson(route, { items: [], total: 0, threshold_minutes: 10 })
    }
    if (path.endsWith('/events')) {
      return fulfillJson(route, { items: [], total: 0, limit: 20, offset: 0 })
    }
    if (path === '/api/v1/errors') {
      return fulfillJson(route, { items: [groupSummary()], total: 1, limit: 20, offset: 0 })
    }
    if (path === `/api/v1/errors/${SUPPORT_GROUP_ID}`) {
      return fulfillJson(route, groupDetail())
    }
    return route.fallback()
  })
}

/**
 * Click the copy control and return the text the clipboard actually holds.
 *
 * `expect.poll` is the wait, not a sleep: the handler awaits
 * `navigator.clipboard.writeText`, so the clipboard can still hold its previous
 * value for a tick after the click. The budgets are generous because a local
 * run shares this machine with other agents' work, and the first assertion
 * after a cold SPA boot is the slow one.
 */
async function clickAndReadClipboard(page: Page, testId: string): Promise<string> {
  const button = page.getByTestId(testId)
  await expect(button).toBeVisible({ timeout: 30_000 })
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await button.click()
  // Read back whatever landed, then assert on it — the raw payload is what a
  // failing run needs to report.
  const read = () => page.evaluate(() => navigator.clipboard.readText())
  await expect
    .poll(read, { message: `[${testId}] never wrote a support reference to the clipboard`, timeout: 30_000 })
    .not.toBe('')
  return read()
}

test.describe('Error surface support references (FAR-2354)', { tag: '@regression' }, () => {
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // The Assistant panel is a position:fixed overlay that can intercept clicks
    // on a row's copy button. Force it closed before the app boots, mirroring
    // the dashboard tests above.
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('the per-row copy control writes the group id, fingerprint, level and count', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockErrorsApi(page)
    await page.goto('/admin/errors')

    const copied = await clickAndReadClipboard(page, `admin-errors-copy-support-${SUPPORT_GROUP_ID}`)

    // Exact payload: no `source` segment (the list payload carries none), no
    // duration, no em dash.
    expect(copied).toBe(
      `error ${SUPPORT_GROUP_ID} · fingerprint ${SUPPORT_GROUP_FINGERPRINT} · level ${SUPPORT_GROUP_LEVEL} · count ${SUPPORT_GROUP_COUNT}`,
    )
    expect(copied).toContain(SUPPORT_GROUP_ID)
    expect(copied).not.toContain('source')
    expect(copied).not.toContain('Duration:')
    expect(copied).not.toContain('—')
  })

  test('the detail copy control adds the reporting source to the same reference', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockErrorsApi(page)
    await page.goto(`/admin/errors/${SUPPORT_GROUP_ID}`)

    const copied = await clickAndReadClipboard(page, 'admin-error-detail-copy-support')

    // Exact payload: the detail payload's `sample_event.source` is appended,
    // so the detail and list references agree on every other field.
    expect(copied).toBe(
      `error ${SUPPORT_GROUP_ID} · fingerprint ${SUPPORT_GROUP_FINGERPRINT} · level ${SUPPORT_GROUP_LEVEL} · count ${SUPPORT_GROUP_COUNT} · source ${SUPPORT_GROUP_SOURCE}`,
    )
    expect(copied).toContain(SUPPORT_GROUP_ID)
    expect(copied).toContain(SUPPORT_GROUP_FINGERPRINT)
    expect(copied).not.toContain('Duration:')
    expect(copied).not.toContain('—')
  })
})
