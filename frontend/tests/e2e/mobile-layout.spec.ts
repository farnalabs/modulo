import { devices, type ConsoleMessage, type Frame, type Page, type Request, type Response } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import yaml from 'js-yaml'
import fs from 'node:fs'
import path from 'node:path'
import { test, expect, loginAsAdmin, setupLocalMockApi } from './setup/fixtures'
import { getTarget, type TestEnv } from './setup/env'
import { WCAG_TAGS, filterViolations } from './setup/wcag'

// Layout checks are deterministic — retrying on flake is noise and triples run
// time against live targets. Top-level scope applies to every test in this file.
test.describe.configure({ retries: 0 })

// Mirror the fixtures.ts mock token values (not exported there) so the local
// auth seed keeps the app's session markers consistent across specs.
const MOCK_ACCESS_TOKEN = 'mock-access-token-for-e2e-tests'
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

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
//
// Kept BELOW the local test budget (FAR-1876) so a dead target trips this
// gate's actionable diagnostic rather than the generic 'Test timeout of Nms
// exceeded'. Local runs drive the unbundled Vite dev server, where a cold
// authenticated route mount is CPU-bound and can take well over 20s on a
// loaded, shared machine (observed), so the bound stays at the 30s the sibling
// specs use and LOCAL_SWEEP_TIMEOUT_MS below supplies the headroom.
const APP_MOUNT_TIMEOUT_MS = 30_000

// Local test budget for the two sweeps that call preparePage(): a cold route
// mount on the dev server can exceed 30s under CPU contention, so the default
// 30s budget would trip the generic deadline before APP_MOUNT_TIMEOUT_MS could
// surface the mount diagnostic. Staging and production serve a pre-built bundle
// and keep the config's 180s default.
const LOCAL_SWEEP_TIMEOUT_MS = 45_000

// ---------------------------------------------------------------------------
// FAR-1890: on-failure mount diagnostics.
//
// The full sweep intermittently fails a route with "did not mount within
// 30000ms" at the FIRST (deliberately unmocked) goto, where the app's bootstrap
// API calls hit the dev proxy with no backend behind it. A bare timeout says
// nothing about WHY `#app` stayed empty, and the competing explanations are
// indistinguishable without evidence:
//
//   a) the module graph never finished loading (a hung/unanswered request);
//   b) Vite re-optimised dependencies mid-sweep and forced a full page reload,
//      so the document was torn down before any component could mount;
//   c) the graph loaded but a bootstrap promise never settled (an unbounded
//      await on the mount critical path, upstream of the router guard);
//   d) an uncaught error aborted the bootstrap path.
//
// So on failure we capture: browser console (incl. the `[vite]` client's own
// lines), page errors, failed responses, requests still in flight at the moment
// of failure, whether `#app` ever had children, the document load count, and the
// `vite:*` window events. The dump is written to stdout and to
// tests/e2e/.mount-diagnostics/.
// ---------------------------------------------------------------------------

const DIAG_DIR = path.join(__dirname, '.mount-diagnostics')
const DIAG_CONSOLE_CAP = 500

interface MountDiagnostics {
  console: string[]
  consoleDropped: number
  pageErrors: string[]
  requestFailed: string[]
  badResponses: string[]
  navigations: string[]
  requestsStarted: number
  requestsFinished: number
  inflight: Map<Request, number>
  dispose: () => void
}

const diagnosticsByPage = new WeakMap<Page, MountDiagnostics>()

// Runs in the page before any application code, on EVERY document load.
// Accumulates across reloads via sessionStorage, so a document that reloads
// repeatedly is distinguishable from one that never loaded at all — the
// decisive signal for candidate (b). `vite:*` events are dispatched by Vite's
// own client, so they distinguish a Vite-triggered reload from any other
// navigation.
const DIAG_INIT_SCRIPT = `
(() => {
  const bump = (key) => {
    try {
      const next = Number(sessionStorage.getItem(key) || '0') + 1
      sessionStorage.setItem(key, String(next))
      return next
    } catch { return 0 }
  }
  window.__diagAppChildMax = 0
  window.__diagNav = bump('__diag_nav')
  for (const evt of ['vite:beforeFullReload', 'vite:ws:disconnect', 'vite:error', 'vite:invalidate', 'vite:ws:connect']) {
    window.addEventListener(evt, () => bump('__diag_' + evt))
  }
  setInterval(() => {
    try {
      const app = document.querySelector('#app')
      const n = app ? app.children.length : 0
      if (n > window.__diagAppChildMax) window.__diagAppChildMax = n
    } catch { /* document torn down mid-poll */ }
  }, 100)
})()
`

// Attach BEFORE the first goto so the initial load is captured in full.
function attachMountDiagnostics(page: Page): MountDiagnostics {
  const existing = diagnosticsByPage.get(page)
  if (existing) return existing

  const diag: MountDiagnostics = {
    console: [],
    consoleDropped: 0,
    pageErrors: [],
    requestFailed: [],
    badResponses: [],
    navigations: [],
    requestsStarted: 0,
    requestsFinished: 0,
    inflight: new Map<Request, number>(),
    dispose: () => {},
  }

  const onConsole = (msg: ConsoleMessage) => {
    if (diag.console.length >= DIAG_CONSOLE_CAP) {
      diag.consoleDropped += 1
      return
    }
    diag.console.push(`[${msg.type()}] ${msg.text()}`)
  }
  const onPageError = (err: Error) => {
    diag.pageErrors.push(err.stack ?? err.message)
  }
  const onRequest = (req: Request) => {
    diag.requestsStarted += 1
    diag.inflight.set(req, Date.now())
  }
  const onRequestFinished = (req: Request) => {
    diag.requestsFinished += 1
    diag.inflight.delete(req)
  }
  const onRequestFailed = (req: Request) => {
    diag.inflight.delete(req)
    diag.requestFailed.push(`${req.method()} ${req.url()} — ${req.failure()?.errorText ?? 'unknown failure'}`)
  }
  const onResponse = (res: Response) => {
    if (res.status() >= 400) diag.badResponses.push(`${res.status()} ${res.url()}`)
  }
  const onFrameNavigated = (frame: Frame) => {
    if (frame === page.mainFrame()) diag.navigations.push(frame.url())
  }

  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('request', onRequest)
  page.on('requestfinished', onRequestFinished)
  page.on('requestfailed', onRequestFailed)
  page.on('response', onResponse)
  page.on('framenavigated', onFrameNavigated)

  diag.dispose = () => {
    page.off('console', onConsole)
    page.off('pageerror', onPageError)
    page.off('request', onRequest)
    page.off('requestfinished', onRequestFinished)
    page.off('requestfailed', onRequestFailed)
    page.off('response', onResponse)
    page.off('framenavigated', onFrameNavigated)
  }

  diagnosticsByPage.set(page, diag)
  return diag
}

interface MountPageState {
  readyState: string
  url: string
  appExists: boolean
  appChildren: number
  appChildMax: number
  navCount: number
  bodyLength: number
  moduleScripts: string[]
  resourceCount: number
  sessionNav: number
  viteBeforeFullReload: number
  viteWsDisconnect: number
  viteWsConnect: number
  viteError: number
  viteInvalidate: number
}

async function collectMountDiagnostics(page: Page, diag: MountDiagnostics, route: string): Promise<string> {
  const lines: string[] = []
  const push = (s: string) => lines.push(s)

  let state: MountPageState | null = null
  let stateError = ''
  try {
    state = await page.evaluate(() => {
      const w = window as unknown as Record<string, unknown>
      const app = document.querySelector('#app')
      const session = (k: string) => {
        try { return Number(sessionStorage.getItem(k) || '0') } catch { return -1 }
      }
      return {
        readyState: document.readyState,
        url: location.href,
        appExists: app !== null,
        appChildren: app ? app.children.length : -1,
        appChildMax: Number(w.__diagAppChildMax ?? -1),
        navCount: Number(w.__diagNav ?? -1),
        bodyLength: document.body ? document.body.innerHTML.length : -1,
        moduleScripts: Array.from(document.querySelectorAll('script[type="module"]'))
          .map(e => (e as HTMLScriptElement).src || '(inline)'),
        resourceCount: performance.getEntriesByType('resource').length,
        sessionNav: session('__diag_nav'),
        viteBeforeFullReload: session('__diag_vite:beforeFullReload'),
        viteWsDisconnect: session('__diag_vite:ws:disconnect'),
        viteWsConnect: session('__diag_vite:ws:connect'),
        viteError: session('__diag_vite:error'),
        viteInvalidate: session('__diag_vite:invalidate'),
      } satisfies MountPageState
    })
  } catch (err) {
    stateError = err instanceof Error ? err.message : String(err)
  }

  push('='.repeat(78))
  push(`[FAR-1890 mount diagnostics] route=${route} at ${new Date().toISOString()}`)
  push('='.repeat(78))

  if (state) {
    push('-- in-page state -----------------------------------------------')
    push(`url:               ${state.url}`)
    push(`readyState:        ${state.readyState}`)
    push(`#app exists:       ${state.appExists}`)
    push(`#app children:     ${state.appChildren}   (max ever seen: ${state.appChildMax})`)
    push(`document load #:   ${state.sessionNav}   (framenavigated events: ${diag.navigations.length})`)
    push(`body innerHTML:    ${state.bodyLength} chars`)
    push(`completed xhr/res: ${state.resourceCount}`)
    push(`module scripts:    ${state.moduleScripts.length}${state.moduleScripts.length > 0 ? ' -> ' + state.moduleScripts.join(', ') : ''}`)
    if (state.sessionNav > 1) {
      push(`*** DOCUMENT RELOADED ${state.sessionNav} TIMES — the page was torn down repeatedly`)
    }
  } else {
    push(`-- in-page state unavailable: ${stateError}`)
  }

  push('-- vite client events (session-cumulative) ---------------------')
  if (state) {
    push(`vite:beforeFullReload=${state.viteBeforeFullReload}  vite:ws:disconnect=${state.viteWsDisconnect}  ` +
      `vite:ws:connect=${state.viteWsConnect}  vite:error=${state.viteError}  vite:invalidate=${state.viteInvalidate}`)
    if (state.viteWsDisconnect > 0 && state.viteWsConnect === 0) {
      push('*** Vite HMR websocket disconnected and never reconnected')
    }
  }

  push('-- navigation events (main frame) ------------------------------')
  if (diag.navigations.length === 0) push('(none recorded)')
  for (const [i, url] of diag.navigations.entries()) push(`${i + 1}. ${url}`)

  push('-- requests still in flight at failure -------------------------')
  const now = Date.now()
  const pending = [...diag.inflight.entries()]
    .map(([req, startedAt]) => ({ url: `${req.method()} ${req.url()}`, ms: now - startedAt }))
    .sort((a, b) => b.ms - a.ms)
  push(`started=${diag.requestsStarted} finished=${diag.requestsFinished} inflight=${pending.length}`)
  if (pending.length === 0) push('(none — nothing was hanging)')
  for (const p of pending) push(`  ${String(p.ms).padStart(7)}ms  ${p.url}`)

  push('-- failed requests ---------------------------------------------')
  if (diag.requestFailed.length === 0) push('(none)')
  for (const r of diag.requestFailed) push(`  ${r}`)

  push('-- responses with status >= 400 --------------------------------')
  if (diag.badResponses.length === 0) push('(none)')
  for (const r of diag.badResponses) push(`  ${r}`)

  push('-- uncaught page errors ----------------------------------------')
  if (diag.pageErrors.length === 0) push('(none)')
  for (const e of diag.pageErrors) push(`  ${e}`)

  const viteConsole = diag.console.filter(l => l.includes('[vite]'))
  push('-- browser console: vite lines ---------------------------------')
  if (viteConsole.length === 0) push('(none)')
  for (const l of viteConsole) push(`  ${l}`)

  push(`-- browser console: all ${diag.console.length} lines (cap ${DIAG_CONSOLE_CAP}, dropped ${diag.consoleDropped}) --`)
  for (const l of diag.console) push(`  ${l}`)

  push('='.repeat(78))
  return lines.join('\n')
}

// Stack-preferred rendering of an unknown thrown value. The dump path runs
// while an unrelated failure is already in flight, so it wants the loudest
// available detail.
function describeError(err: unknown): string {
  return err instanceof Error ? (err.stack ?? err.message) : String(err)
}

// BEST-EFFORT BY CONTRACT (FAR-1890): this is called from inside the
// mount-failure catch, so it must never reject. A rejection here would REPLACE
// the mount error being reported and destroy the very evidence this exists to
// collect, so every step is guarded and a dump failure is reported on the
// console instead of propagating.
async function writeMountDiagnostics(page: Page, route: string, reason: string): Promise<void> {
  try {
    const diag = diagnosticsByPage.get(page)
    if (!diag) return
    let report: string
    try {
      report = await collectMountDiagnostics(page, diag, route)
    } catch (err) {
      report = `[FAR-1890] diagnostics collection itself failed for ${route}:\n${describeError(err)}`
    }
    let file = '(not written)'
    try {
      fs.mkdirSync(DIAG_DIR, { recursive: true })
      file = path.join(DIAG_DIR, `${route.replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_|_$/g, '') || 'root'}-${Date.now()}.log`)
      fs.writeFileSync(file, `reason: ${reason}\n${report}\n`)
    } catch (err) {
      file = `(write failed: ${describeError(err)})`
    }
    process.stdout.write(`\n${report}\ndiagnostics file: ${file}\n\n`)
  } catch (err) {
    // Never rethrow — see the contract above.
    console.warn(`[FAR-1890] mount diagnostics dump failed: ${describeError(err)}`)
  } finally {
    try {
      diagnosticsByPage.get(page)?.dispose()
    } catch (err) {
      // Listener teardown is best-effort; the page may already be gone.
      console.warn(`[FAR-1890] listener teardown failed: ${describeError(err)}`)
    }
  }
}

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
    // The dump must never MASK the mount error below — that error is the signal
    // this whole diagnostic exists to serve. A failing dump is reported loudly
    // and then stepped over, so the original failure always propagates (FAR-1890).
    try {
      await writeMountDiagnostics(page, route, `did not mount within ${APP_MOUNT_TIMEOUT_MS}ms`)
    } catch (dumpErr) {
      console.warn(`[FAR-1890] mount diagnostics dump threw: ${describeError(dumpErr)}`)
    }
    throw new Error(
      `[mobile-layout] ${route} did not mount within ${APP_MOUNT_TIMEOUT_MS}ms (${page.url()}) — ` +
        'the target returned an error/non-SPA page; aborting route checks. ' +
        '(FAR-1890: full mount diagnostics dumped above and under tests/e2e/.mount-diagnostics/)',
    )
  }
}

// Navigate, wait for the Vue app to mount and data to settle. A silent skip is
// never acceptable (FAR-1850): the caller either runs the checks, or the route
// was redirected by a gate listed in DEACTIVATED_ROUTES (backed by its
// per-route reason) — anything else is an assertion failure, not a skip.
type PageReadiness = 'rendered' | 'deactivated'

async function preparePage(page: Page, route: string, env: TestEnv): Promise<PageReadiness> {
  // FAR-1890: attach BEFORE the first goto so the initial load — the load the
  // intermittent failure lands on — is captured in full.
  await page.addInitScript({ content: DIAG_INIT_SCRIPT })
  attachMountDiagnostics(page)

  const response = await page.goto(route)

  await waitForAppMount(page, route, response)

  // No content settle here (FAR-1876). A blanket
  // `waitForSelector('[data-loading="false"]')` only matches on the two routes
  // that render the marker (AnalyticsView and SettingsLicenseView); on every
  // other route the wait burned its full 15s timeout and was then discarded by
  // `.catch(() => {})`. Two of those (32.3s incl. navigation) overran the 30s
  // local test budget, the deadline fired mid-wait,
  // and the discarded rejection let the test limp into checkViewportMeta()
  // during teardown — reported as a viewport-meta failure (`Received string:
  // ""`) with no trace of the real cause. There is no universal content marker
  // to wait on; `waitForAppMount` above is the readiness signal. A future
  // settle MUST throw on timeout rather than swallow — a swallowed settle
  // failure is indistinguishable from the page being wrong.

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
    }
  } else if (page.url().includes('/login') && route !== '/login') {
    // storageState may have expired — fall back to a single real login, then
    // retry the route and re-run the mount/settle waits before proceeding.
    await loginAsAdmin(page, env)
    const retry = await page.goto(route)
    await waitForAppMount(page, route, retry)
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
test.describe('Mobile layout audit — exclusion config integrity', { tag: ['@regression', '@mobile'] }, () => {
  test('DEACTIVATED_ROUTES lists only live manifest routes, each with a reason', { tag: ['@regression', '@mobile'] }, () => {
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
  if (target === 'local') test.describe.configure({ timeout: LOCAL_SWEEP_TIMEOUT_MS })
  for (const route of ROUTES) {
    test(`${route} — mobile layout invariants + above-the-fold screenshot`, { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
      await runFullSweep(page, route, env)
    })
  }
})

test.describe('Mobile layout audit — narrow viewport bounding sweep', { tag: '@mobile' }, () => {
  if (target === 'local') test.describe.configure({ timeout: LOCAL_SWEEP_TIMEOUT_MS })
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
