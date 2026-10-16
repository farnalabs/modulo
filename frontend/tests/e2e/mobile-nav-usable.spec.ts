import { devices, type Page } from '@playwright/test'
import { test, expect, loginAsAdmin, setupLocalMockApi } from './setup/fixtures'
import { getTarget, type TestEnv } from './setup/env'

// FAR-1805 — the mobile nav must be USABLE, not just present.
//
// Up to now the mobile-nav specs asserted first-paint layout correctness
// (`mobile-nav-flash.spec.ts`) and viewport layout invariants
// (`mobile-layout.spec.ts`). Neither proves that the chrome a mobile session
// actually gets settles to exactly one nav component AND can be used to
// navigate. The flags read (the caller-scoped `GET /api/v1/plan` bootstrap
// since FAR-1809) is not guaranteed readable for the e2e identity on every
// target; FAR-1805's terminal fallback (flagsSource 'unavailable' →
// hamburger drawer) is what makes the mobile nav workable there, so this spec
// is written chrome-agnostic: it derives the chrome from the DOM and accepts
// either the drawer or the icon rail, asserting against the LIVE claim (the
// flag payload) only when that payload is actually readable.
//
// Design constraints:
//  - the settle waits are auto-retrying assertions, never fixed sleeps;
//  - the pending placeholder may legitimately never paint (rail from cache),
//    so first paint is logged, not asserted — the discriminator is that it
//    must DRAIN (never park forever, the pre-fix failure mode);
//  - the mode-consistency test skips LOUDLY (reason written to stdout) while
//    the flags endpoint is unreadable for the e2e identity — a documented
//    capability gate, not a silent skip.

test.describe.configure({ retries: 0 })

test.use({ ...devices['Pixel 5'], deviceScaleFactor: 1 })

// Non-local targets reuse the single global-setup login
// (storageState-staging.json) the same way mobile-layout.spec.ts does.
if (getTarget() !== 'local') {
  test.use({ storageState: 'storageState-staging.json' })
}

// Mirror the fixtures.ts mock token values (not exported there) so the local
// auth seed keeps the app's session markers consistent across specs.
const MOCK_ACCESS_TOKEN = 'mock-access-token-for-e2e-tests'
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

const HAMBURGER = '[aria-controls="mobile-sidebar"]'
const RAIL = '[aria-label="Expand sidebar"]'
// Kept as a literal (not imported from src/composables) so this e2e module
// does not pull the browser-only app graph (localStorage) into the Node loader.
const PENDING = '[data-testid="mobile-nav-pending"]'
const FLAG_NAME = 'mobile_sidebar_rail'

// Discriminator for the pre-fix failure mode: before FAR-1805 an unreadable
// flags read parked the neutral nav placeholder forever. FAR-1805 makes the
// fallback terminal, so the placeholder must drain. Generous bound sized for
// the fallback path, in line with the app-mount budget, never wall-clock.
const SETTLE_TIMEOUT_MS = 30_000
const NAV_TIMEOUT_MS = 15_000
// A bounded mount wait keeps one dead target from consuming the whole 90-min
// staging E2E job (same rationale as mobile-layout.spec.ts).
const APP_MOUNT_TIMEOUT_MS = 30_000
// Probe at most the first N visible nav links for one that navigates elsewhere.
const MAX_PROBED_LINKS = 8

interface ChromeCounts {
  hamburger: number
  rail: number
  pending: number
}

async function chromeCounts(page: Page): Promise<ChromeCounts> {
  return page.evaluate(([hamburger, rail, pending]) => ({
    hamburger: document.querySelectorAll(hamburger).length,
    rail: document.querySelectorAll(rail).length,
    pending: document.querySelectorAll(pending).length,
  }), [HAMBURGER, RAIL, PENDING] as [string, string, string])
}

async function chromeNavSum(page: Page): Promise<number> {
  const counts = await chromeCounts(page)
  return counts.hamburger + counts.rail
}

const APPS_MOUNTED = () => (document.querySelector('#app')?.children.length ?? 0) > 0

/**
 * Navigate to `/` as an authenticated session (mocked tokens + mock API on
 * local targets; storageState with a single real-login retry on live ones) and
 * wait for the Vue app to mount. Both chrome flavours settle afterwards
 * through auto-retrying assertions, never wall-clock sleeps.
 */
async function boot(page: Page, env: TestEnv): Promise<void> {
  // Cold Vite dev compile + a CPU-contended machine can push the first
  // navigation well past the config's 30s local budget; the interesting
  // assertions all happen after mount, so give the boot its own headroom.
  test.setTimeout(90_000)
  if (env.name === 'local') {
    await setupLocalMockApi(page)
    await page.addInitScript(([token, refresh]) => {
      localStorage.setItem('modulo_access_token', token)
      localStorage.setItem('modulo_refresh_token', refresh)
    }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN] as [string, string])
    await page.goto('/', { waitUntil: 'commit' })
  } else {
    await page.goto('/', { waitUntil: 'commit' })
    if (page.url().includes('/login')) {
      // storageState expired — fall back to one real login, then retry.
      await loginAsAdmin(page, env)
      await page.goto('/')
    }
  }
  await page.waitForFunction(APPS_MOUNTED, undefined, { timeout: APP_MOUNT_TIMEOUT_MS })
}

/**
 * Wait for the mobile nav to resolve: the placeholder must be GONE and exactly
 * one chrome (hamburger drawer OR icon rail) present. Returns the settled
 * counts. Failure messages name the pre-FAR-1805 failure mode explicitly.
 */
async function settleChrome(page: Page): Promise<ChromeCounts> {
  await expect(
    page.locator(PENDING),
    'the pending placeholder must drain once the flags read settles — never parked forever (the pre-FAR-1805 failure mode)',
  ).toHaveCount(0, { timeout: SETTLE_TIMEOUT_MS })

  await expect.poll(() => chromeNavSum(page), {
    timeout: SETTLE_TIMEOUT_MS,
    message: 'exactly one mobile chrome (hamburger drawer OR icon rail) must settle — never zero, never both',
  }).toBe(1)

  const settled = await chromeCounts(page)
  process.stdout.write(`[far-1805] settled chrome: ${JSON.stringify(settled)}\n`)
  return settled
}

/**
 * Open the nav panel for the given settled chrome mode, then navigate to a
 * different page using one of the panel's own visible links.
 */
async function openAndNavigate(page: Page, mode: 'rail' | 'drawer'): Promise<void> {
  const trigger = page.locator(mode === 'rail' ? RAIL : HAMBURGER).first()
  await expect(trigger, `the settled ${mode} trigger must be present and tappable`).toBeVisible()
  await trigger.click()

  const panel = mode === 'rail'
    ? page.locator('aside[role="dialog"]')
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
    await page.waitForURL((url) => new URL(url).pathname === targetPath, { timeout: NAV_TIMEOUT_MS })
    expect(page, 'navigation must land on the SPA, never bounce to the login page')
      .not.toHaveURL(/\/login/)
    process.stdout.write(`[far-1805] usable-nav check (${mode}): ${currentPath} -> ${targetPath}\n`)
    return
  }
  throw new Error(
    `[far-1805] all ${candidateCount} visible nav links in the ${mode} panel point back to ` +
    `${currentPath} — the panel offers no onward navigation`,
  )
}

type FlagRead =
  | { readable: true; railActive: boolean }
  | { readable: false; reason: string }

interface FlagsPayload {
  flags?: Array<{ name?: string; currently_active?: unknown }>
}

function isFlagsPayload(body: unknown): body is FlagsPayload {
  return typeof body === 'object' && body !== null
    && Array.isArray((body as FlagsPayload).flags)
}

/**
 * Read the mobile_sidebar_rail flag from the caller-scoped plan bootstrap
 * (`GET /api/v1/plan`, the app's flag source since FAR-1809 — it replaced the
 * instance-admin `/admin/feature-flags` read) through an in-page fetch (a
 * page-context request, so Playwright route interception still applies on the
 * local mock target) using the app's own Bearer header convention. Never
 * throws — every failure path returns an unreadable FlagRead with a reason.
 */
async function readRailFlag(page: Page): Promise<FlagRead> {
  try {
    const result = await page.evaluate(async () => {
      let token: string | null
      try {
        token = localStorage.getItem('modulo_access_token')
      } catch (err) {
        return { status: 0, bodyText: '', error: `localStorage unavailable: ${err instanceof Error ? err.message : String(err)}` }
      }
      if (!token) return { status: 0, bodyText: '', error: 'no modulo_access_token in localStorage' }
      try {
        const response = await fetch('/api/v1/plan', {
          headers: { Authorization: `Bearer ${token}` },
        })
        const bodyText = await response.text()
        return { status: response.status, bodyText, error: null }
      } catch (err) {
        return { status: 0, bodyText: '', error: `fetch failed: ${err instanceof Error ? err.message : String(err)}` }
      }
    })
    if (result.error) return { readable: false, reason: result.error }

    if (result.status !== 200) {
      const hint = result.status === 403
        ? ' — the e2e identity cannot read the plan bootstrap, so this check stays skipped until it is readable'
        : ''
      return { readable: false, reason: `plan bootstrap returned HTTP ${result.status}${hint}` }
    }
    let body: unknown = null
    try { body = JSON.parse(result.bodyText) } catch { body = null }
    if (!isFlagsPayload(body)) {
      return {
        readable: false,
        reason: `plan bootstrap body was not a flags payload (first 120 chars: ${result.bodyText.slice(0, 120)})`,
      }
    }
    const flag = body.flags?.find((f) => f?.name === FLAG_NAME)
    return { readable: true, railActive: Boolean(flag?.currently_active) }
  } catch (err) {
    return { readable: false, reason: `page.evaluate failed: ${err instanceof Error ? err.message : String(err)}` }
  }
}

test.describe('FAR-1805 — the mobile nav is usable, not just present', { tag: ['@regression', '@mobile'] }, () => {
  test('the pending placeholder drains and exactly one mobile chrome settles', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    await boot(page, env)

    // The placeholder may legitimately never paint (rail resolved from
    // cache); first paint is a diagnostic log, not an assertion.
    const firstPaint = await chromeCounts(page)
    process.stdout.write(`[far-1805] first paint: ${JSON.stringify(firstPaint)}\n`)

    const settled = await settleChrome(page)
    expect(settled.pending, 'the placeholder must be gone once resolved').toBe(0)
  })

  test('the settled mobile chrome can navigate to another page', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    await boot(page, env)
    const settled = await settleChrome(page)

    // settleChrome guaranteed hamburger+rail === 1, so the mode is exactly
    // whichever selector is mounted.
    const mode: 'rail' | 'drawer' = settled.rail > 0 ? 'rail' : 'drawer'
    await openAndNavigate(page, mode)
  })

  test('the settled chrome matches the live mobile_sidebar_rail flag when the flag is readable', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    await boot(page, env)
    const settled = await settleChrome(page)

    const read = await readRailFlag(page)
    if (!read.readable) {
      process.stdout.write(`[far-1805] SKIP (flag unreadable): ${read.reason}\n`)
      test.skip(true, `feature flags unreadable — mode check stays skipped until it is readable: ${read.reason}`)
      return
    }
    const expected = read.railActive
      ? { hamburger: 0, rail: 1, pending: 0 }
      : { hamburger: 1, rail: 0, pending: 0 }
    expect(settled, `the settled chrome must match the live flag (railActive=${read.railActive})`)
      .toEqual(expected)
  })
})
