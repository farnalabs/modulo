import type { Page } from '@playwright/test'
import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * Run-detail metadata block + heartbeat rendering (FAR-1839).
 *
 * Two behaviours pinned end to end, on the real layout rather than jsdom:
 *  - a TERMINAL run whose `heartbeat_at` the backend keeps (only a re-dispatch
 *    clears it) shows the heartbeat's absolute local time — never "—", never
 *    the "(stale)" marker, which is a live-run signal only — while a terminal
 *    run genuinely missing a heartbeat still shows "—";
 *  - the five metadata fields render as an aligned label/value definition
 *    list: on desktop every label shares one left edge and every value
 *    shares another, and on a phone viewport the block never overflows.
 *
 * All API traffic is page.route-mocked (mirrors json-viewer.spec.ts), so the
 * spec is deterministic against any target.
 */

const RUN_ID = 'run-meta-terminal'

/** The kept heartbeat on the terminal-run fixture (FAR-1839). */
const HEARTBEAT_AT = '2026-08-01T10:04:55Z'

/**
 * The value the component renders for `HEARTBEAT_AT`, derived INSIDE the page
 * with the same `toLocaleString` call the view makes (formatTimestamp →
 * heartbeatDisplayText). Deriving it in the browser keeps the assertion
 * independent of the runner's timezone: the fixture instant is
 * 10:04:55 UTC on Aug 1, which renders "Jul 31" anywhere west of UTC-11 —
 * a hardcoded /Aug 1/ regex was timezone-dependent (FAR-1839 follow-up).
 * Mirrors the unit test's derivation in RunDetailView.spec.ts.
 */
async function expectedHeartbeatText(page: Page): Promise<string> {
  return page.evaluate(
    (iso) =>
      new Date(iso).toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }),
    HEARTBEAT_AT,
  )
}

function terminalRun(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: RUN_ID,
    run_number: 7,
    pipeline_id: 'p-1',
    pipeline_name: 'Metadata Test',
    status: 'complete',
    trigger_type: 'manual',
    trigger_actor: 'admin@example.com',
    created_at: '2026-08-01T10:00:00Z',
    started_at: '2026-08-01T10:00:05Z',
    completed_at: '2026-08-01T10:05:00Z',
    error_code: null,
    total_cost_usd: 0.01,
    ...overrides,
  }
}

// Registered AFTER loginAsAdmin (which installs the catch-all mock), so these
// specific routes take precedence over it for this run id.
async function mockRun(page: Page, run: Record<string, unknown>): Promise<void> {
  await page.route(`**/api/v1/runs/${RUN_ID}`, (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(run) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await page.route(`**/api/v1/runs/${RUN_ID}/io`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ outputs_json: null, input_payload: null }) }),
  )
  await page.route(`**/api/v1/runs/${RUN_ID}/hitl/pending`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ reviews: [] }) }),
  )
  await page.route(`**/api/v1/runs/${RUN_ID}/events*`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) }),
  )
}

async function openRunDetail(page: Page, run: Record<string, unknown>): Promise<void> {
  await mockRun(page, run)
  await page.goto(`/runs/${RUN_ID}`)
  await expect(page.getByTestId('run-detail-heartbeat')).toBeVisible({ timeout: 30_000 })
}

/** The metadata definition list = the dl that owns the heartbeat row. */
function metadataList(page: Page) {
  return page.locator('dl').filter({ has: page.getByTestId('run-detail-heartbeat') })
}

test.describe('Run detail metadata + heartbeat (FAR-1839)', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server can exceed the 30s default before any assertion runs.
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // Keep the Assistant floating panel from overlaying the page (it renders
    // where dev-mode is on). Mirrors json-viewer.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('a terminal run shows its kept absolute last heartbeat, not a dash', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await openRunDetail(page, terminalRun({ heartbeat_at: HEARTBEAT_AT }))

    const heartbeat = page.getByTestId('run-detail-heartbeat')
    await expect(heartbeat).toContainText('Last heartbeat')
    // The value is a formatted local timestamp, not the "—" placeholder.
    await expect(heartbeat).not.toContainText('—')
    await expect(heartbeat).toContainText(await expectedHeartbeatText(page))
    // Staleness is a live-run signal only — a finished run never wears it.
    await expect(heartbeat).not.toContainText('(stale)')
    await expect(page.getByTestId('run-detail-warnings-strip-stale-heartbeat')).toHaveCount(0)
  })

  test('a terminal run without a heartbeat still renders the dash', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await openRunDetail(page, terminalRun({ heartbeat_at: null }))

    const heartbeat = page.getByTestId('run-detail-heartbeat')
    await expect(heartbeat).toContainText(/Last heartbeat\s+—/)
  })

  test('renders all five metadata fields with their labels', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await openRunDetail(page, terminalRun({ heartbeat_at: HEARTBEAT_AT }))

    const list = metadataList(page)
    await expect(list).toBeVisible()
    await expect(list).toContainText('Created')
    await expect(list).toContainText('Started')
    await expect(list).toContainText('Completed')
    const actor = page.getByTestId('run-detail-trigger-actor')
    await expect(actor).toContainText('Triggered by')
    await expect(actor).toContainText('admin@example.com')
    await expect(list).toContainText('Last heartbeat')
    await expect(list.locator('dt')).toHaveCount(5)
    await expect(list.locator('dd')).toHaveCount(5)
  })

  test('metadata labels and values align in two columns on desktop', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await openRunDetail(page, terminalRun({ heartbeat_at: HEARTBEAT_AT }))

    const list = metadataList(page)
    // One shared left edge for every label, one for every value — this is the
    // alignment the pre-FAR-1839 wrapping inline list could not provide.
    const labelXs = await list.locator('dt').evaluateAll((els) =>
      els.map((el) => Math.round(el.getBoundingClientRect().x)),
    )
    const valueXs = await list.locator('dd').evaluateAll((els) =>
      els.map((el) => Math.round(el.getBoundingClientRect().x)),
    )
    expect(new Set(labelXs).size, `labels not aligned: ${JSON.stringify(labelXs)}`).toBe(1)
    expect(new Set(valueXs).size, `values not aligned: ${JSON.stringify(valueXs)}`).toBe(1)
    expect(valueXs[0], 'values sit after the label column').toBeGreaterThan(labelXs[0])
  })

  test('the metadata block stays inside a phone viewport', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.setViewportSize({ width: 375, height: 812 })
    await openRunDetail(page, terminalRun({ heartbeat_at: HEARTBEAT_AT }))

    const list = metadataList(page)
    // All five rows still readable (no collapsed/hidden labels)...
    await expect(list.locator('dt')).toHaveCount(5)
    await expect(list).toContainText('Last heartbeat')
    // ...and the block itself never scrolls horizontally.
    const box = await list.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
    expect(box.scrollWidth, `metadata block overflows: ${JSON.stringify(box)}`).toBeLessThanOrEqual(box.clientWidth + 1)
  })
})
