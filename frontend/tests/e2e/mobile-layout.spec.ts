import { devices, type Page, type Response } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import yaml from 'js-yaml'
import fs from 'node:fs'
import path from 'node:path'
import { test, expect, loginAsAdmin, setupLocalMockApi } from './setup/fixtures'
import { getTarget, type TestEnv } from './setup/env'

// Layout checks are deterministic — retrying on flake is noise and triples run
// time against live targets. Top-level scope applies to every test in this file.
test.describe.configure({ retries: 0 })

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

const ACCEPTABLE_VIOLATIONS = ['color-contrast', 'scrollable-region-focusable']

// Mirror the fixtures.ts mock token values (not exported there) so the local
// auth seed keeps the app's session markers consistent across specs.
const MOCK_ACCESS_TOKEN = 'mock-access-token-for-e2e-tests'
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

function filterViolations(violations: { id: string }[]) {
  return violations.filter(v => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}

// Above-the-fold screenshots land here for a later LLM visual review.
const CAPTURE_DIR = path.join(__dirname, '.mobile-captures')
fs.mkdirSync(CAPTURE_DIR, { recursive: true })

const FALLBACK_ROUTES = ['/login', '/', '/pipelines', '/schemas', '/admin/connectors', '/admin/model-backends']

// Enumerate route paths from the manifest at spec load; fall back to a fixed
// list when the manifest cannot be read or yields nothing.
function enumerateRoutes(): string[] {
  try {
    const manifestPath = path.join(__dirname, '../../src/manifest.yaml')
    const manifest = yaml.load(fs.readFileSync(manifestPath, 'utf-8')) as { routes?: Record<string, unknown> }
    const routes = Object.keys(manifest.routes ?? {})
      .filter(p => p.startsWith('/'))
      .filter(p => p !== '/login')
      .filter(p => !p.includes('oauth') && !p.includes('/auth/'))
      .filter(p => !p.includes('://'))
    if (routes.length > 0) {
      return routes
    }
  } catch (err) {
    process.stdout.write(`[mobile-layout] manifest.yaml enumeration failed (${err instanceof Error ? err.message : String(err)}); using fallback route list\n`)
  }
  return FALLBACK_ROUTES
}

const ROUTES = enumerateRoutes()
process.stdout.write(`[mobile-layout] enumerated ${ROUTES.length} routes from manifest.yaml\n`)

// Routes the sweep is ALLOWED not to render, mapped to the specific app gate
// that deactivates them. This is the ONLY skip category: any other non-forward
// navigation — including a redirect to /login after the real-login retry —
// fails the test. Derived empirically (FAR-1850): every entry below was
// observed redirecting on staging or local, and its mechanism cross-checked
// against the router guard (frontend/src/router/index.ts
// enforceRoleTierVisibility + the feature_flag gate). The two environments
// differ in three plan/auth facts:
//   staging: real admin login → org_role=admin (roles pass), plan >= team
//     (tier passes), real flags. Dev mode is ON for the staging org as of
//     2026-10-16 (observed: every private_preview route renders for the e2e
//     admin; server-side org flag change, not a code deploy — the deploy of
//     FAR-1809 that day failed), so private_preview deactivates nothing.
//   local:   mock token 'mock-access-token-for-e2e-tests' is NOT a JWT, so
//     decodeJwtPayload returns null → every required_roles route redirects;
//     the mock feature-flag response returns flags: [], so every
//     feature_flag-gated route redirects; dev_mode: true renders
//     private_preview routes.
// A listed route that actually renders still runs every check — the map only
// licenses the skip when the route is genuinely redirected. Maintaining this
// list: a new route may ONLY be added together with the app gate that
// redirects it (private_preview / system_admin_only / tier / role / flag),
// never as a blanket "skip it". Re-validate entries against BOTH environments
// before trusting them — this map has already drifted once (the preview
// entries above were removed the day staging's dev-mode flag flipped on).
const DEACTIVATED_ROUTES: Record<string, string> = {
  // --- system_admin_only (FAR-938): SYSTEM sidebar group or route-entry flag;
  // no e2e identity (staging org admin or local mock) carries is_system_admin ---
  '/admin/product-analytics': 'system_admin_only — route entry flag; e2e identities have no is_system_admin claim',
  '/admin/system/config': 'system_admin_only — route entry flag; e2e identities have no is_system_admin claim',
  '/admin/system/orgs': 'system_admin_only — route entry flag; e2e identities have no is_system_admin claim',
  '/admin/housekeeping': 'system_admin_only — SYSTEM sidebar group; e2e identities have no is_system_admin claim',
  '/admin/run-retention': 'system_admin_only — SYSTEM sidebar group; e2e identities have no is_system_admin claim',
  '/settings/license': 'system_admin_only — SYSTEM sidebar group; e2e identities have no is_system_admin claim',
  '/settings/email': 'system_admin_only — SYSTEM sidebar group; e2e identities have no is_system_admin claim',
  '/settings/observability': 'system_admin_only — SYSTEM sidebar group',
  '/admin/feature-flags': 'system_admin_only — SYSTEM sidebar group; also required_roles (local mock token is not a JWT)',
  '/settings/error-forwarders': 'system_admin_only — SYSTEM sidebar group; also required_roles',
  '/settings/rate-limits': 'system_admin_only — SYSTEM sidebar group; also required_roles',
  '/settings/runtime-config': 'system_admin_only — SYSTEM sidebar group; also required_roles',
  // --- private_preview: documented mechanism, currently deactivates nothing.
  // Both e2e identities run with dev mode on (local mock dev_mode: true;
  // staging org flag observed on 2026-10-16), so preview routes render in
  // both environments. The 5 routes that were listed here (/runs/diff,
  // /settings/monitoring, /feedback/inbox, /assistant, /settings/assistant)
  // were removed after they were observed RENDERING on staging; if the
  // staging dev-mode flag is turned off again they must come back.
  // --- feature_flag inactive for the staging e2e identity ---
  '/admin/notification-delivery': "feature_flag 'webhook_notification_log' inactive for the staging e2e identity; local mock also lacks org_role (required_roles)",
  // --- required_roles [admin]: the local mock token is not a JWT, so the
  // guard cannot read org_role and redirects; the staging identity is a real
  // org admin and renders these ---
  '/admin/users': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/org': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/audit': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/connectors': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/costs': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/costs/components': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/costs/controls': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/costs/limits': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/errors': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/errors/:id': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/model-backends': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/parameter-schemas': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/runners/concurrency': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/runners/profiles': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/runners/profiles/new': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/runners/profiles/:id/edit': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/trigger-events': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/settings/teams': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/settings/sso': "required_roles [admin] — local mock token is not a JWT (no org_role claim); staging renders this",
  '/admin/assistant': "required_roles [admin] — local mock token is not a JWT",
  '/admin/node-categories': "required_roles [admin] — local mock token is not a JWT",
  '/admin/views': "required_roles [admin] — local mock token is not a JWT",
  '/admin/plugins': "required_roles [admin] — local mock token is not a JWT",
}

const DEACTIVATED_ROUTES_SET = new Set(Object.keys(DEACTIVATED_ROUTES))

const NARROW_ROUTES = ['/login', '/', '/pipelines', '/schemas']

function sanitizePath(p: string): string {
  return p.replace(/[^a-z0-9-]/gi, '_').replace(/^_+|_+$/g, '') || 'root'
}

// Canvas/editor routes (Vue Flow canvases, effectively infinite height) render
// so much content that a full-page screenshot can exhaust the Playwright
// worker and crash the whole run ("worker process exited unexpectedly"). All
// deterministic invariant checks still run — only the screenshot capture is
// skipped on these routes. Matches manifest entries like /pipelines/:id/editor,
// /composites/:id/editor, /evals/editor, /schemas/editor/:id.
function isCanvasRoute(route: string): boolean {
  return route.includes('/editor') || route.includes('/composites/')
}

// Bound the app-mount wait. The full sweep walks every manifest route, so an
// unbounded wait (Playwright defaults to the 180s test timeout) turns one dead
// target into 71 x 180s = ~3.5h, overrunning the 90-min staging-E2E job and
// getting the run cancelled with no actionable signal. A hung run is reported
// as 'cancelled' and re-dispatches the Branch Fixer, so fail fast and loud
// instead.
const APP_MOUNT_TIMEOUT_MS = 30_000

// Wait for the Vue app to mount, failing fast with the HTTP status whenever the
// target serves an error/non-SPA page instead of the app shell. A 5xx edge
// response (the app is down) is detected immediately rather than after the
// mount timeout.
async function waitForAppMount(page: Page, route: string, response: Response | null): Promise<void> {
  if (response && response.status() >= 500) {
    throw new Error(
      `[mobile-layout] ${route} returned HTTP ${response.status()} (${page.url()}) — ` +
        'the target is not serving the SPA; aborting route checks.',
    )
  }
  try {
    await page.waitForFunction(
      () => (document.querySelector('#app')?.children.length ?? 0) > 0,
      undefined,
      { timeout: APP_MOUNT_TIMEOUT_MS },
    )
  } catch {
    throw new Error(
      `[mobile-layout] ${route} did not mount within ${APP_MOUNT_TIMEOUT_MS}ms (${page.url()}) — ` +
        'the target returned an error/non-SPA page; aborting route checks.',
    )
  }
}

// Navigate, wait for the Vue app to mount and data to settle. A silent skip is
// never acceptable (FAR-1850): the caller either runs the checks, or the route
// was redirected by a gate listed in DEACTIVATED_ROUTES (backed by its
// per-route reason) — anything else is an assertion failure, not a skip.
type PageReadiness = 'rendered' | 'deactivated'

async function preparePage(page: Page, route: string, env: TestEnv): Promise<PageReadiness> {
  const response = await page.goto(route)
  await page.waitForURL('**/*', { timeout: 5000 }).catch(() => {})

  await waitForAppMount(page, route, response)

  // Content settle — not every route renders a data-loading marker.
  await page.waitForSelector('[data-loading="false"]', { timeout: 15000 }).catch(() => {})

  if (env.name === 'local') {
    // Local CI: mock all /api/v1 traffic and seed the auth tokens so
    // authenticated routes render with mocked data (mirrors loginAsAdmin's
    // local branch). Re-navigate so the router guard picks up the seeded
    // session; /login stays untouched so the login page renders as-is.
    await setupLocalMockApi(page)
    if (route !== '/login') {
      await page.evaluate(([token, refresh]) => {
        localStorage.setItem('modulo_access_token', token)
        localStorage.setItem('modulo_refresh_token', refresh)
      }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN])
      const remount = await page.goto(route)
      await waitForAppMount(page, route, remount)
      await page.waitForSelector('[data-loading="false"]', { timeout: 15000 }).catch(() => {})
    }
  } else if (page.url().includes('/login') && route !== '/login') {
    // storageState may have expired — fall back to a single real login, then
    // retry the route and re-run the mount/settle waits before proceeding.
    await loginAsAdmin(page, env)
    const retry = await page.goto(route)
    await waitForAppMount(page, route, retry)
    await page.waitForSelector('[data-loading="false"]', { timeout: 15000 }).catch(() => {})
  }

  const finalUrl = page.url()
  const differsFromPath = !finalUrl.includes(route)
  if (!differsFromPath) return 'rendered'

  if (DEACTIVATED_ROUTES_SET.has(route)) {
    process.stdout.write(`  [mobile-layout] deactivated: ${route} — ${DEACTIVATED_ROUTES[route]} (redirected to ${finalUrl})\n`)
    return 'deactivated'
  }

  // A redirect the config does not own: silent-skip ban means this FAILS, and
  // the message names the only auditable way forward (add to DEACTIVATED_ROUTES
  // with the app gate as reason). This also covers the historic silent class —
  // a redirect to /login AFTER the real-login retry above already ran, i.e. a
  // genuinely unauthenticated session.
  expect(
    differsFromPath,
    `${route} redirected to ${finalUrl} instead of rendering. Silent sweep skips are forbidden (FAR-1850): either fix the redirect (session expired? broken login retry?) or add this route to DEACTIVATED_ROUTES with the app gate that excludes it (private_preview / system_admin_only / requiredTier / requiredRoles) as a per-route reason.`,
  ).toBe(false)
  return 'deactivated' // unreachable — expect throws first
}

// Check 1 — viewport meta must opt into device-width rendering.
async function checkViewportMeta(page: Page) {
  const meta = page.locator('meta[name="viewport"]').first()
  await expect(meta).toHaveAttribute('content', /width=device-width/, {
    message: 'No <meta name="viewport" content="width=device-width, ...> found — page renders at 980px desktop width scaled down to ~38% on phones.',
  })
}

// Check 2 — no horizontal page overflow; log suspected 100vw-width culprits.
// Note: viewport comparisons use document.documentElement.clientWidth, NOT
// window.innerWidth — innerWidth inflates to the content width when the page
// has horizontal overflow in mobile emulation, so it would mask real overflow.
async function checkNoHorizontalOverflow(page: Page) {
  const result = await page.evaluate(() => {
    const doc = document.documentElement
    const vw = document.documentElement.clientWidth
    const overflow = doc.scrollWidth > vw + 1
    let culprits: string[] = []
    if (overflow) {
      // computed width:100vw resolves to viewport width + scrollbar, so elements
      // wider than the visible viewport are the classic scrollbar-overflow cause
      // Sample the first 200 elements — enrichment is advisory, and
      // getComputedStyle per element is the slow part on a large DOM.
      culprits = Array.from(document.querySelectorAll('*'))
        .slice(0, 200)
        .filter(el => {
          const w = Number.parseFloat(getComputedStyle(el).width)
          return !Number.isNaN(w) && w > vw
        })
        .slice(0, 10)
        .map(el => `<${el.tagName.toLowerCase()}> width=${getComputedStyle(el).width}`)
    }
    return { overflow, scrollWidth: doc.scrollWidth, viewportWidth: vw, culprits }
  })
  if (result.overflow) {
    process.stdout.write(`[mobile-layout] horizontal overflow: scrollWidth=${result.scrollWidth} viewportWidth=${result.viewportWidth}\n`)
    if (result.culprits.length > 0) {
      process.stdout.write(`[mobile-layout]   suspected width:100vw culprits: ${result.culprits.join(', ')}\n`)
    }
  }
  expect(result.overflow, `Horizontal page overflow (scrollWidth ${result.scrollWidth} > viewportWidth ${result.viewportWidth})`).toBe(false)
}

// Check 3 — app shell fills the viewport width; main-content ratio is advisory.
async function checkAppShellFillsViewport(page: Page) {
  const data = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth
    const app = document.querySelector('#app')
    let shellRect = app?.getBoundingClientRect()
    if (!shellRect || shellRect.width === 0) {
      let widest: Element | null = null
      let widestWidth = 0
      for (const el of Array.from(document.body.children)) {
        const r = el.getBoundingClientRect()
        if (r.width > widestWidth) {
          widestWidth = r.width
          widest = el
        }
      }
      shellRect = widest?.getBoundingClientRect()
    }
    const appRatio = shellRect && shellRect.width > 0 ? shellRect.width / vw : 0
    let mainRatio = 0
    for (const el of Array.from(document.querySelectorAll('main, [role="main"]'))) {
      const r = el.getBoundingClientRect()
      if (r.width > 0) mainRatio = Math.max(mainRatio, r.width / vw)
    }
    return { appRatio, mainRatio }
  })
  process.stdout.write(`[mobile-layout] ${page.url()} app shell width ratio: ${data.appRatio.toFixed(2)}, widest main container ratio: ${data.mainRatio.toFixed(2)}\n`)
  expect(data.appRatio, `App shell fills ${(data.appRatio * 100).toFixed(0)}% of the viewport width — background likely does not fill the screen`).toBeGreaterThanOrEqual(0.95)
}

// The only interactive element permitted to sit partially clipped at the mobile
// viewport is the small floating Assistant launcher button: it is a fixed-position
// FAB whose persisted position may legitimately rest off-screen on narrow
// screens, and it is always reachable by tapping. The expected clipped count is
// DERIVED at runtime from the allowlisted elements actually present on the page
// — never a hardcoded magic number. Any OTHER clipped interactive element is a
// real clipping bug and fails the check.
const CLIPPED_ALLOWLIST_SELECTOR = '.assistant-floating-btn'

// Check 4 — visible interactive elements must not be clipped off-screen.
async function checkInteractiveNotClipped(page: Page) {
  const result = await page.evaluate((allowlistSelector) => {
    const doc = document.documentElement
    const vw = document.documentElement.clientWidth
    const hasHScroll = doc.scrollWidth > vw + 1
    if (hasHScroll) {
      return { skip: true, clipped: [], allowlistedClipped: [], allowlistCount: 0, sampled: false, interactiveCount: 0 }
    }
    // A right-side overhang inside a horizontal scroll container (e.g. a wide
    // table with overflow-x: auto) is reachable by scrolling the container —
    // not a clipping bug. Walk the ancestor chain (bounded).
    // Bounded ancestor walk — 30 steps covers realistic wrapper nesting while
    // keeping per-element work O(30) instead of walking to the document root.
    function hasHorizontalScrollableAncestor(el: Element): boolean {
      let node = el.parentElement
      let depth = 0
      while (node && depth < 30) {
        const overflowX = getComputedStyle(node).overflowX
        if (overflowX === 'auto' || overflowX === 'scroll') return true
        node = node.parentElement
        depth += 1
      }
      return false
    }
    const selector = 'button, a, input, select, textarea, [tabindex], [role="button"]'
    const interactives = Array.from(document.querySelectorAll(selector))
    const clipped: { tag: string; cls: string; text: string; left: number; right: number }[] = []
    const allowlistedClipped: { tag: string; cls: string; text: string; left: number; right: number }[] = []
    for (const el of interactives.slice(0, 500)) {
      const htmlEl = el as HTMLElement
      if (htmlEl.offsetParent === null) continue
      if (getComputedStyle(htmlEl).visibility === 'hidden') continue
      const rect = htmlEl.getBoundingClientRect()
      if (rect.width === 0 && rect.height === 0) continue
      const overhangsRight = rect.right > vw + 1
      if (!(rect.left < -1 || overhangsRight)) continue
      // Closed off-canvas drawer (e.g. the mobile sidebar translated fully
      // left): contents sit off-screen in their correct closed state and are
      // reachable when the drawer opens — never a clipping bug.
      if (rect.right <= 0) continue
      // Closed off-canvas panel resting fully off-screen right (e.g. the
      // draggable Assistant floating panel whose persisted position can sit
      // outside the viewport): reachable when dragged back — never a
      // clipping bug.
      if (rect.left >= window.innerWidth) continue
      // Right-side overhang reachable by scrolling a horizontal container.
      if (overhangsRight && hasHorizontalScrollableAncestor(htmlEl)) continue
      const entry = {
        tag: htmlEl.tagName.toLowerCase(),
        cls: typeof htmlEl.className === 'string' ? htmlEl.className.slice(0, 80) : '',
        text: (htmlEl.textContent ?? '').trim().slice(0, 60),
        left: Math.round(rect.left),
        right: Math.round(rect.right),
      }
      if (htmlEl.matches(allowlistSelector)) {
        allowlistedClipped.push(entry)
      } else {
        clipped.push(entry)
      }
    }
    return {
      skip: false,
      clipped: clipped.slice(0, 10),
      allowlistedClipped: allowlistedClipped.slice(0, 10),
      // Derived expected-tolerance count: the allowlisted elements actually
      // present on the page (typically the one Assistant FAB). Used so the
      // assertion reads against reality instead of a magic literal.
      allowlistCount: document.querySelectorAll(allowlistSelector).length,
      sampled: interactives.length > 500,
      interactiveCount: interactives.length,
    }
  }, CLIPPED_ALLOWLIST_SELECTOR)
  if (result.skip) {
    process.stdout.write(`[mobile-layout] ${page.url()} has horizontal scroll — skipping clipped-interactive check\n`)
    return
  }
  if (result.sampled) {
    process.stdout.write(`[mobile-layout] clipped scan sampled: ${result.interactiveCount} interactives, processing first 500\n`)
  }
  if (result.clipped.length > 0) {
    for (const c of result.clipped) {
      process.stdout.write(`[mobile-layout]   clipped interactive: <${c.tag}> class="${c.cls}" text="${c.text}" left=${c.left} right=${c.right}\n`)
    }
  }
  if (result.allowlistedClipped.length > 0) {
    for (const c of result.allowlistedClipped) {
      console.log(`[mobile-layout]   allowlisted clipped (${CLIPPED_ALLOWLIST_SELECTOR}): <${c.tag}> class="${c.cls}" left=${c.left} right=${c.right}`)
    }
  }
  expect(
    result.clipped,
    `Interactive elements clipped off-screen (${result.clipped.length}); expected clipped count is ${result.allowlistCount} (allowlisted ${CLIPPED_ALLOWLIST_SELECTOR} element(s) present). Unexpected: ${JSON.stringify(result.clipped)}`,
  ).toEqual([])
}

// Check 5 — axe WCAG AA at the mobile viewport.
async function checkAxeMobile(page: Page) {
  // Pathological pages (e.g. the admin housekeeping candidate list) render
  // thousands of elements; running axe over that DOM exceeds the test timeout.
  const domSize = await page.evaluate(() => document.querySelectorAll('*').length)
  if (domSize > 8000) {
    process.stdout.write(`[mobile-layout] skipping axe on ${page.url()} (DOM too large: ${domSize} elements)\n`)
    return
  }
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
  const violations = filterViolations(results.violations)
  if (violations.length > 0) {
    process.stdout.write(`\n=== ${page.url()} mobile WCAG violations ===\n`)
    for (const v of violations) {
      process.stdout.write(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}\n`)
    }
  }
  expect(violations).toEqual([])
}

// Check 6 — cumulative layout shift over a ~1s settle period. ADVISORY only:
// CLS is a data- and timing-dependent Web Vitals performance metric (affected
// by late API responses, fonts, polling, and measurement-window placement), not
// a deterministic layout-formatting invariant like overflow/clipping/shell-fill,
// which remain hard gates. Values above 0.25 are reported for Lighthouse/field
// follow-up rather than failing the sweep.
async function checkCLS(page: Page) {
  const cls = await page.evaluate(() => {
    return new Promise<number>(resolve => {
      let total = 0
      try {
        const observer = new PerformanceObserver(list => {
          for (const entry of list.getEntries()) {
            const shift = entry as { hadRecentInput?: boolean; value?: number }
            if (!shift.hadRecentInput && typeof shift.value === 'number') {
              total += shift.value
            }
          }
        })
        observer.observe({ type: 'layout-shift', buffered: true })
        setTimeout(() => {
          observer.disconnect()
          resolve(total)
        }, 1000)
      } catch {
        resolve(total)
      }
    })
  })
  if (cls > 0.25) {
    process.stdout.write(`[mobile-layout] CLS ${cls.toFixed(3)} on ${page.url()} (ADVISORY: > 0.25 — data/timing-dependent; fix in Lighthouse/field metrics)\n`)
  } else if (cls > 0.1) {
    process.stdout.write(`[mobile-layout] ADVISORY: CLS ${cls.toFixed(3)} on ${page.url()} (0.1 < CLS <= 0.25)\n`)
  } else {
    process.stdout.write(`[mobile-layout] CLS ${cls.toFixed(3)} on ${page.url()}\n`)
  }
}

// Check 7 — advisory only: inputs under 16px trigger iOS auto-zoom.
async function checkInputFontSize(page: Page) {
  const small = await page.evaluate(() => {
    const found: string[] = []
    for (const el of Array.from(document.querySelectorAll('input, select, textarea'))) {
      const htmlEl = el as HTMLElement
      if (htmlEl.offsetParent === null) continue
      const size = Number.parseFloat(getComputedStyle(htmlEl).fontSize)
      if (!Number.isNaN(size) && size < 16) {
        found.push(`<${htmlEl.tagName.toLowerCase()}> ${size}px class="${typeof htmlEl.className === 'string' ? htmlEl.className.slice(0, 60) : ''}"`)
      }
    }
    return found.slice(0, 10)
  })
  if (small.length > 0) {
    process.stdout.write(`[mobile-layout] ADVISORY: inputs with font-size < 16px (iOS auto-zoom risk) on ${page.url()}:\n`)
    for (const s of small) {
      process.stdout.write(`[mobile-layout]   ${s}\n`)
    }
  }
}

async function runFullSweep(page: Page, route: string, env: TestEnv) {
  const readiness = await preparePage(page, route, env)
  test.skip(
    readiness === 'deactivated',
    `${route} is not renderable by this identity — see DEACTIVATED_ROUTES for the per-route gate reason`,
  )

  await checkViewportMeta(page)
  await checkNoHorizontalOverflow(page)
  await checkAppShellFillsViewport(page)
  await checkInteractiveNotClipped(page)
  await checkAxeMobile(page)
  await checkCLS(page)
  await checkInputFontSize(page)

  if (isCanvasRoute(route)) {
    process.stdout.write(`[mobile-layout] skipping screenshot for canvas route ${route}\n`)
  } else {
    await page.screenshot({ path: path.join(CAPTURE_DIR, `${sanitizePath(route)}.png`) })
  }
}

async function runNarrowChecks(page: Page, route: string, env: TestEnv) {
  const readiness = await preparePage(page, route, env)
  test.skip(
    readiness === 'deactivated',
    `${route} is not renderable by this identity — see DEACTIVATED_ROUTES for the per-route gate reason`,
  )

  await checkViewportMeta(page)
  await checkNoHorizontalOverflow(page)
  await checkInteractiveNotClipped(page)
}

// Base emulation for every test in this file (overridden per nested describe for
// the narrow sweep's extra viewports).
test.use({ ...devices['Pixel 5'], deviceScaleFactor: 1 })

const target = getTarget()
// Non-local targets reuse the single global-setup login (storageState-staging.json)
// so all 82 routes share one session instead of 82 individual logins (avoids
// production rate limits). The file is written to the CWD (frontend/) by
// global-setup.ts; keep this path plain-relative.
if (target !== 'local') {
  test.use({ storageState: 'storageState-staging.json' })
}

// Config integrity: the only allowed skip category must name ROUTES that still
// exist and carry a real gate reason — otherwise the sweep would silently
// accumulate a stale, unreasoned exclusion list over time.
test.describe('Mobile layout audit — exclusion config integrity', () => {
  test('DEACTIVATED_ROUTES lists only live manifest routes, each with a reason', () => {
    const stale = [...DEACTIVATED_ROUTES_SET].filter(r => !ROUTES.includes(r))
    expect(
      stale.length,
      `DEACTIVATED_ROUTES contains routes no longer swept by the manifest (stale exclusions do not belong in the config): ${stale.join(', ')}`,
    ).toBe(0)
    for (const [route, reason] of Object.entries(DEACTIVATED_ROUTES)) {
      expect(reason.length, `DEACTIVATED_ROUTES['${route}'] must carry a per-route gate reason`).toBeGreaterThan(0)
    }
  })
})

test.describe('Mobile layout audit — full route sweep', { tag: ['@regression', '@mobile'] }, () => {
  for (const route of ROUTES) {
    test(`${route} — mobile layout invariants + above-the-fold screenshot`, { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
      await runFullSweep(page, route, env)
    })
  }
})

test.describe('Mobile layout audit — narrow viewport bounding sweep', { tag: '@mobile' }, () => {
  // defaultBrowserType is stripped from the Pixel 5 spread: Playwright forbids
  // use({ defaultBrowserType }) inside a describe group (it forces a new worker).
  const pixel5 = devices['Pixel 5']
  const narrowViewports = [
    { name: 'pixel5', viewport: pixel5.viewport, userAgent: pixel5.userAgent, screen: pixel5.screen, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
    { name: '320x568', viewport: { width: 320, height: 568 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
    { name: '768x1024', viewport: { width: 768, height: 1024 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
  ]

  for (const vp of narrowViewports) {
    test.describe(`viewport ${vp.name}`, () => {
      test.use(vp)
      for (const route of NARROW_ROUTES) {
        test(`${route} — no horizontal overflow / clipped controls`, { tag: '@mobile' }, async ({ page, env }) => {
          await runNarrowChecks(page, route, env)
        })
      }
    })
  }
})
