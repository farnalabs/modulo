import { devices, type Page, type Response } from '@playwright/test'
import { test, expect, setupLocalMockApi } from './setup/fixtures'
import { flagCacheKey, serializeFlagCache } from '../../src/config/flagCache'

// FAR-1237 — first-paint layout correctness on mobile under a SLOW
// feature-flags response.
//
// Before the fix, `mobile_sidebar_rail` was read from a store that started
// empty and only populated after `GET /api/v1/admin/feature-flags` landed, so
// every mobile load painted the legacy hamburger top-nav first and swapped to
// the left rail when the response arrived (the slow-connection flash Duncan
// reported). The fix resolves the mode from a persisted cache synchronously
// and renders a neutral placeholder while it is genuinely unknown.
//
// The flags request is GATED (held until the test releases it) rather than
// time-delayed, so the first-paint assertions are deterministic.
//
// Local-only: relies on the mock API plus an overriding route.

test.describe.configure({ retries: 0 })
test.use({ ...devices['Pixel 5'], deviceScaleFactor: 1 })

const MOCK_ACCESS_TOKEN = 'mock-access-token-for-e2e-tests'
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

const HAMBURGER = '[aria-controls="mobile-sidebar"]'
const RAIL = '[aria-label="Expand sidebar"]'
// Kept as a literal (not imported from src/composables) so this e2e module
// does not pull the browser-only app graph (localStorage) into the Node loader.
const PENDING = '[data-testid="mobile-nav-pending"]'

interface ChromeCounts {
  hamburger: number
  rail: number
  pending: number
}

async function chrome(page: Page): Promise<ChromeCounts> {
  return page.evaluate(([hamburger, rail, pending]) => ({
    hamburger: document.querySelectorAll(hamburger).length,
    rail: document.querySelectorAll(rail).length,
    pending: document.querySelectorAll(pending).length,
  }), [HAMBURGER, RAIL, PENDING] as [string, string, string])
}

interface ShiftRecord {
  value: number
  nodes: string[]
}

interface ShiftReport {
  cls: number
  /** Largest shifts first, with their source nodes — CLS instrumentation. */
  top: ShiftRecord[]
}

async function readShifts(page: Page): Promise<ShiftReport> {
  return page.evaluate(() => {
    const w = window as unknown as { __far1237Cls?: number; __far1237Shifts?: ShiftRecord[] }
    const top = [...(w.__far1237Shifts ?? [])]
      .sort((a, b) => b.value - a.value)
      .slice(0, 3)
    return { cls: w.__far1237Cls ?? 0, top }
  })
}

async function flushTwoFrames(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
  }))
}

interface FlagGate {
  /** Release the held feature-flags response. */
  release: () => void
  /** Resolves when the held flags response has been delivered. */
  response: Promise<Response>
}

/**
 * Boot the app at `/` with an authenticated session, the local mock API, and a
 * feature-flags response that is HELD until `release()` is called. The cached
 * flag map (FAR-1237 persistence) is optionally seeded before any page script
 * runs, modelling a returning user.
 */
async function boot(page: Page, opts: { rail: boolean; cachedRail?: boolean }): Promise<FlagGate> {
  // Cold Vite dev compile + a CPU-contended machine can push the first
  // navigation well past the config's 30s local budget; the interesting
  // assertions all happen after mount, so give the boot its own headroom.
  test.setTimeout(90_000)
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })

  await setupLocalMockApi(page)
  // Registered after the catch-all mock — Playwright gives later-registered
  // handlers precedence, so this one owns the flags endpoint.
  await page.route('**/api/v1/admin/feature-flags', async (route) => {
    await gate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        flags: [
          {
            name: 'mobile_sidebar_rail',
            description: 'Left icon rail on mobile',
            tier: 'team',
            currently_active: opts.rail,
            depends_on: null,
          },
        ],
        license: { tier: 'enterprise' },
        dev_mode: true,
      }),
    })
  })

  const response = page.waitForResponse(
    (r) => r.url().includes('/api/v1/admin/feature-flags') && r.status() === 200,
  )
  // If a first-paint assertion fails before release(), this promise would
  // reject unhandled at the response timeout and muddy the failure output.
  void response.catch(() => undefined)

  await page.addInitScript(([token, refresh]) => {
    localStorage.setItem('modulo_access_token', token)
    localStorage.setItem('modulo_refresh_token', refresh)
  }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN] as [string, string])

  await page.addInitScript(() => {
    type LayoutShiftLike = PerformanceEntry & {
      hadRecentInput?: boolean
      value?: number
      sources?: Array<{ node?: Node | null }>
    }
    const w = window as unknown as { __far1237Cls?: number; __far1237Shifts?: ShiftRecord[] }
    w.__far1237Cls = 0
    w.__far1237Shifts = []
    try {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          const shift = entry as LayoutShiftLike
          if (shift.hadRecentInput || typeof shift.value !== 'number') continue
          w.__far1237Cls += shift.value
          if (shift.value <= 0.01) continue
          const nodes = (shift.sources ?? []).map((source) => {
            const node = source.node
            if (!node) return '<detached>'
            const el = node as Element
            const cls = typeof el.className === 'string' && el.className
              ? `.${el.className.trim().split(/\s+/).slice(0, 3).join('.')}`
              : ''
            return `${(node.nodeName || '?').toLowerCase()}${cls}`.slice(0, 70)
          })
          w.__far1237Shifts.push({ value: shift.value, nodes })
        }
      })
      observer.observe({ type: 'layout-shift', buffered: true })
    } catch (err) {
      // layout-shift unsupported — CLS stays 0 (advisory metric only).
      console.warn('[far-1237] layout-shift observer unavailable', err)
    }
  })

  if (opts.cachedRail !== undefined) {
    const payload = serializeFlagCache({ mobile_sidebar_rail: opts.cachedRail })
    // The mock access token is opaque (no decodable org_id claim), so the
    // store reads the shared `unknown` bucket — seed the same key it will use.
    await page.addInitScript(([key, raw]) => {
      localStorage.setItem(key, raw)
    }, [flagCacheKey(null), payload] as [string, string])
  }

  // 'commit' — don't gate on the full load event (external font stylesheet /
  // cold compile); readiness is the app mount wait below.
  await page.goto('/', { waitUntil: 'commit' })
  await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)
  return { release, response }
}

test.describe('FAR-1237 — mobile first-paint layout under a slow feature-flags response', { tag: ['@mobile'] }, () => {
  test.beforeEach(({ env }) => {
    test.skip(env.name !== 'local', 'held-flag mocks require the local mock API')
  })

  // CLS is advisory (timing/data-dependent), but printing it on EVERY run —
  // including failures — is what makes the before/after comparison possible.
  // The top shift sources identify which element actually moved (e.g. the
  // legacy header ↔ left-rail swap).
  test.afterEach(async ({ page }) => {
    try {
      const { cls, top } = await readShifts(page)
      process.stdout.write(`[far-1237] CLS total (advisory): ${cls.toFixed(3)}\n`)
      for (const shift of top) {
        process.stdout.write(
          `[far-1237]   shift ${shift.value.toFixed(3)} from: ${shift.nodes.join(' | ') || '<no sources>'}\n`,
        )
      }
    } catch (err) {
      // Page already gone — no CLS to report.
      console.warn('[far-1237] CLS read skipped (page closed)', err)
    }
  })

  // Flow order: capture first paint → release the flags → settle → THEN
  // assert. A pre-fix failure still completes the whole session, so the
  // recorded diagnostics and CLS include the header↔rail swap itself rather
  // than aborting before it happens.
  test('first visit (rail org): pending placeholder paints first, the legacy top-nav never appears', { tag: '@mobile' }, async ({ page }) => {
    const gate = await boot(page, { rail: true })

    const firstPaint = await chrome(page)
    process.stdout.write(`[far-1237] first paint, no cache, flag ON: ${JSON.stringify(firstPaint)}\n`)

    gate.release()
    await gate.response
    await flushTwoFrames(page)

    const settled = await chrome(page)
    process.stdout.write(`[far-1237] after flags resolve ON: ${JSON.stringify(settled)}\n`)

    expect(firstPaint.pending, 'the neutral placeholder must paint while the flag is unknown').toBe(1)
    expect(firstPaint.hamburger, 'the legacy top-nav must never paint for a rail org').toBe(0)
    expect(firstPaint.rail, 'the rail must not paint before the flag resolves').toBe(0)
    expect(settled).toEqual({ hamburger: 0, rail: 1, pending: 0 })
  })

  test('first visit (drawer org): pending placeholder paints first, the rail never appears', { tag: '@mobile' }, async ({ page }) => {
    const gate = await boot(page, { rail: false })

    const firstPaint = await chrome(page)
    process.stdout.write(`[far-1237] first paint, no cache, flag OFF: ${JSON.stringify(firstPaint)}\n`)

    gate.release()
    await gate.response
    await flushTwoFrames(page)

    const settled = await chrome(page)
    process.stdout.write(`[far-1237] after flags resolve OFF: ${JSON.stringify(settled)}\n`)

    expect(firstPaint.pending).toBe(1)
    expect(firstPaint.hamburger, 'the legacy top-nav must not paint before the flag resolves').toBe(0)
    expect(firstPaint.rail, 'the rail must never paint for a drawer org').toBe(0)
    expect(settled).toEqual({ hamburger: 1, rail: 0, pending: 0 })
  })

  test('returning rail user: the left rail paints first from the persisted cache, even while flags are slow', { tag: '@mobile' }, async ({ page }) => {
    const gate = await boot(page, { rail: true, cachedRail: true })

    const firstPaint = await chrome(page)
    process.stdout.write(`[far-1237] first paint, cached rail: ${JSON.stringify(firstPaint)}\n`)

    gate.release()
    await gate.response
    await flushTwoFrames(page)

    const settled = await chrome(page)
    process.stdout.write(`[far-1237] after slow flags resolve ON: ${JSON.stringify(settled)}\n`)

    expect(firstPaint, 'the resolved layout itself must paint first — no placeholder, no legacy top-nav')
      .toEqual({ hamburger: 0, rail: 1, pending: 0 })
    expect(settled, 'the layout must not flip once resolved').toEqual({ hamburger: 0, rail: 1, pending: 0 })
  })

  test('returning drawer user (secondary UI): the top-nav paints first from cache and is never swapped for the rail', { tag: '@mobile' }, async ({ page }) => {
    const gate = await boot(page, { rail: false, cachedRail: false })

    const firstPaint = await chrome(page)
    process.stdout.write(`[far-1237] first paint, cached drawer: ${JSON.stringify(firstPaint)}\n`)

    gate.release()
    await gate.response
    await flushTwoFrames(page)

    const settled = await chrome(page)
    process.stdout.write(`[far-1237] after slow flags resolve OFF: ${JSON.stringify(settled)}\n`)

    expect(firstPaint, 'the secondary UI must also paint its resolved layout first')
      .toEqual({ hamburger: 1, rail: 0, pending: 0 })
    expect(settled, 'the secondary layout must not be replaced by the rail').toEqual({ hamburger: 1, rail: 0, pending: 0 })
  })
})
