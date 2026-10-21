import { devices, type Page, type Response } from '@playwright/test'
import { test, expect, setupLocalMockApi } from './setup/fixtures'
import { flagCacheKey, serializeFlagCache } from '../../src/config/flagCache'

// FAR-1237 — first-paint layout correctness on mobile under a SLOW
// feature-flags response.
//
// Before the fix, `mobile_sidebar_rail` was read from a store that started
// empty and only populated after the tier-gated bootstrap landed (then
// `GET /api/v1/admin/feature-flags`; repointed to the caller-scoped
// `GET /api/v1/plan` by FAR-1809), so
// every mobile load painted the legacy hamburger top-nav first and swapped to
// the left rail when the response arrived (the slow-connection flash Duncan
// reported). The fix resolves the mode from a persisted cache synchronously.
//
// Under the current boot architecture the first navigation is tier-gated:
// `enforceRoleTierVisibility` (`src/router/index.ts`) awaits
// `planStore.fetchPlan()` before `main.ts`'s `router.isReady()` can resolve —
// and `main.ts` mounts the app only after that. So while the flags response
// is genuinely unknown, the SPA itself is not yet mounted: `#app` stays empty
// and NO nav chrome can exist. The observable first-visit in-flight invariant
// is therefore "nothing paints at all" — the legacy top-nav or the rail can
// never be the wrong first paint for it. The neutral placeholder
// (`mobile-nav-pending`, kept as `PENDING` below) only exists once a layout
// is mounted and the store is still 'none' — which the gate-driven first
// visit can never produce; its real-world guarantee (draining instead of
// parking forever, and the terminal 'unavailable' → drawer fallback) is
// asserted end-to-end by `mobile-nav-usable.spec.ts` (FAR-1805) on targets
// where the flags read is unreadable.
//
// The flags request is GATED (held until the test releases it) rather than
// time-delayed, so both phases are deterministic: phase 1 samples chrome
// while the response is held (mount is structurally impossible then, e.g.
// during the 15s `withTimeout` budget), phase 2 releases, waits for the app
// mount, and asserts the resolved layout. `flushTwoFrames` uses rAF only —
// never a fixed sleep.
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

// FAR-1805: a settled chrome must also be USABLE, not just present. Open the
// nav panel for the test's known mode and navigate to a different page via one
// of the panel's own visible links. Duplicated here rather than shared with
// mobile-nav-usable.spec.ts — importing across spec files would execute the
// other file's `test.describe` registrations.
const USABLE_NAV_TIMEOUT_MS = 15_000
// Probe at most the first N visible nav links for one that navigates elsewhere
// (kept in step with mobile-nav-usable.spec.ts).
const MAX_PROBED_LINKS = 8

async function openAndNavigate(page: Page, mode: 'rail' | 'drawer'): Promise<void> {
  const trigger = page.locator(mode === 'rail' ? RAIL : HAMBURGER).first()
  await expect(trigger, `the settled ${mode} trigger must be present and tappable`).toBeVisible()
  await trigger.click()

  const panel = mode === 'rail'
    ? page.locator("#mobile-nav-panel")
    : page.locator('#mobile-sidebar')
  await expect(panel, `the ${mode}-mode nav panel must open on tap`).toBeVisible()

  // Scope to the opened panel: in rail mode the in-flow icon rail also renders
  // `.sidebar-link`s (SidebarRail -> SidebarNav). A page-wide locator can
  // resolve `.first()` to a rail link sitting behind the modal backdrop rather
  // than one inside the opened panel.
  const links = panel.locator('.sidebar-link:visible')
  await expect(links.first(), 'the opened panel must expose at least one visible nav link').toBeVisible()

  const currentPath = new URL(page.url()).pathname
  const candidateCount = Math.min(await links.count(), MAX_PROBED_LINKS)
  for (let i = 0; i < candidateCount; i += 1) {
    const href = await links.nth(i).getAttribute('href')
    // Only consider in-app router links; anything else is not navigation.
    if (!href || !href.startsWith('/')) continue
    const targetPath = new URL(href, page.url()).pathname
    if (targetPath === currentPath) continue
    await links.nth(i).click()
    await page.waitForURL((url) => new URL(url).pathname === targetPath, {
      timeout: USABLE_NAV_TIMEOUT_MS,
    })
    expect(page, 'navigation must land on the SPA, never bounce to the login page')
      .not.toHaveURL(/\/login/)
    process.stdout.write(`[far-1237+1805] usable-nav check (${mode}): ${currentPath} -> ${targetPath}\n`)
    return
  }
  throw new Error(
    `[far-1237+1805] all ${candidateCount} visible nav links in the ${mode} panel point back to ` +
    `${currentPath} — the panel offers no onward navigation`,
  )
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

// Boot bounds, sized for a cold Vite dev compile on a CPU-contended machine.
// The flags request fires only after the module graph evaluates (post-compile),
// so the request wait gets generous headroom; everything after release is
// ms-scale and stays well inside the 90s per-test budget.
const FLAGS_BOOT_TIMEOUT_MS = 45_000
// Generous bound for the post-release mount wait — auto-retried by
// waitForFunction's rAF polling, never a fixed sleep (mirrors the 30s settle
// budget in mobile-nav-usable.spec.ts).
const SETTLE_TIMEOUT_MS = 30_000

/**
 * Boot the app at `/` with an authenticated session, the local mock API, and a
 * feature-flags response that is HELD until `release()` is called. The cached
 * flag map (FAR-1237 persistence) is optionally seeded before any page script
 * runs, modelling a returning user.
 *
 * `waitForMount` (default true) selects which readiness the boot awaits:
 *  - true — the app has mounted (`#app` has children), so layout assertions
 *    can sample immediately (the cached-returning-user tests).
 *  - false — only the flags REQUEST has fired: the router's tier guard is
 *    awaiting `planStore.fetchPlan()` (whose flags leg is the held request)
 *    and `main.ts` awaits `router.isReady()` before mounting. This is the
 *    genuinely-unknown window the first-visit tests sample in phase 1.
 *    The response stays HELD in both modes — release() is still required.
 */
async function boot(page: Page, opts: { rail: boolean; cachedRail?: boolean; waitForMount?: boolean }): Promise<FlagGate> {
  // Cold Vite dev compile + a CPU-contended machine can push the first
  // navigation well past the config's 30s local budget; the interesting
  // assertions all happen after mount, so give the boot its own headroom.
  test.setTimeout(90_000)
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })

  await setupLocalMockApi(page)
  // Registered after the catch-all mock — Playwright gives later-registered
  // handlers precedence, so this one owns the plan bootstrap endpoint
  // (`GET /api/v1/plan`, the tier-gated flags source since FAR-1809).
  await page.route('**/api/v1/plan', async (route) => {
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
        // Real catalog tier (community/team). An invented 'enterprise' rank is
        // absent from a real deployment's catalog, which would fail the
        // dashboard route's community required_tier and self-redirect-loop the
        // guard; see notifications.spec.ts for the staging incident.
        license: { tier: 'team' },
        dev_mode: true,
      }),
    })
  })

  const response = page.waitForResponse(
    (r) => r.url().includes('/api/v1/plan') && r.status() === 200,
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
  if (opts.waitForMount === false) {
    // Phase-1 boot: stop once the guard's flags fetch is genuinely in flight.
    // The route handler is still awaiting the gate, so the response stays held
    // and the store keeps resolving.
    await page.waitForRequest(
      (r) => r.url().includes('/api/v1/plan'),
      { timeout: FLAGS_BOOT_TIMEOUT_MS },
    )
    return { release, response }
  }
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

  // First visit = no persisted cache. Both tests follow the same two-phase,
  // gate-driven structure:
  //   Phase 1 (response held): the router's tier guard
  //   (`enforceRoleTierVisibility`) awaits planStore.fetchPlan(), whose flags
  //   leg is the held request; `main.ts` awaits `router.isReady()` before
  //   mounting — so the SPA is structurally NOT mounted and no nav chrome (not
  //   even the neutral placeholder, which lives inside AppLayout) can exist.
  //   The in-flight invariant is therefore "nothing paints at all": neither
  //   chrome can ever be the wrong first paint. A regression that lets layout
  //   render pre-settle trips this phase-1 zero or the settled assertion.
  //   Phase 2 (release): the response resolves, the app mounts, and the
  //   resolved layout paints; the settled chrome must match the flag exactly.
  // The placeholder's real-world behaviour — draining once the store resolves
  // (never parked forever), and the terminal 'unavailable' → drawer fallback —
  // is asserted end-to-end by mobile-nav-usable.spec.ts (FAR-1805).
  test('first visit (rail org): nothing paints while the flag is unknown; the rail paints once it resolves', { tag: '@mobile' }, async ({ page }) => {
    const gate = await boot(page, { rail: true, waitForMount: false })

    const whileFlagsUnknown = await chrome(page)
    process.stdout.write(`[far-1237] while flag unknown, no cache, flag ON: ${JSON.stringify(whileFlagsUnknown)}\n`)

    expect(
      whileFlagsUnknown,
      'no nav chrome may exist while the flag is unknown — the SPA is not yet mounted (the tier guard is awaiting the held flags fetch)',
    ).toEqual({ hamburger: 0, rail: 0, pending: 0 })

    gate.release()
    await gate.response
    await page.waitForFunction(
      () => (document.querySelector('#app')?.children.length ?? 0) > 0,
      undefined,
      { timeout: SETTLE_TIMEOUT_MS },
    )
    await flushTwoFrames(page)

    const settled = await chrome(page)
    process.stdout.write(`[far-1237] after flags resolve ON: ${JSON.stringify(settled)}\n`)

    expect(settled, 'the resolved layout must paint once the flag lands').toEqual({ hamburger: 0, rail: 1, pending: 0 })

    // FAR-1805: the settled rail must also be usable, not just present.
    await openAndNavigate(page, 'rail')
  })

  test('first visit (drawer org): nothing paints while the flag is unknown; the drawer paints once it resolves', { tag: '@mobile' }, async ({ page }) => {
    const gate = await boot(page, { rail: false, waitForMount: false })

    const whileFlagsUnknown = await chrome(page)
    process.stdout.write(`[far-1237] while flag unknown, no cache, flag OFF: ${JSON.stringify(whileFlagsUnknown)}\n`)

    expect(
      whileFlagsUnknown,
      'no nav chrome may exist while the flag is unknown — the SPA is not yet mounted (the tier guard is awaiting the held flags fetch)',
    ).toEqual({ hamburger: 0, rail: 0, pending: 0 })

    gate.release()
    await gate.response
    await page.waitForFunction(
      () => (document.querySelector('#app')?.children.length ?? 0) > 0,
      undefined,
      { timeout: SETTLE_TIMEOUT_MS },
    )
    await flushTwoFrames(page)

    const settled = await chrome(page)
    process.stdout.write(`[far-1237] after flags resolve OFF: ${JSON.stringify(settled)}\n`)

    expect(settled, 'the resolved layout must paint once the flag lands').toEqual({ hamburger: 1, rail: 0, pending: 0 })

    await openAndNavigate(page, 'drawer')
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

    await openAndNavigate(page, 'rail')
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

    await openAndNavigate(page, 'drawer')
  })
})
