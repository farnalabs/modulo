/**
 * polish-capture — the canonical headless capture for the `polish` UI-pass skill.
 *
 * WHAT THIS IS
 *   A capture tool, NOT an assertion suite. It screenshots ONE route at three
 *   viewports and writes `measurements.json`. The only assertions are that the
 *   capture itself succeeded (screenshots + measurements written), so a broken
 *   capture fails loudly instead of silently producing nothing. It never
 *   asserts visual correctness — adjudication against the polish checklist is
 *   the skill's job, downstream of this spec.
 *
 *   It is INERT in CI: without `POLISH_ROUTE` every test in this file is
 *   skipped, so the spec adds zero cost and zero flake to the normal e2e
 *   suite, and a capture run can never fail a CI job.
 *
 * EXACT INVOCATION (run from `frontend/` in the repo/worktree):
 *
 *   # Local target (default). Playwright's own `webServer` config starts and
 *   # stops the Vite dev server for you (frontend/playwright.config.ts), and
 *   # the fixtures mock all /api/v1 traffic — no backend required.
 *   # NOTE: name the file with its .spec.ts suffix — Playwright matches bare
 *   # positional filters as regexes against ABSOLUTE paths, so a substring
 *   # that appears in the checkout's own directory name (e.g. a worktree
 *   # called `polish-capture`) would match every spec in the tree:
 *   POLISH_ROUTE=/pipelines pnpm exec playwright test polish-capture.spec.ts
 *
 *   # Optional output subdirectory (defaults to the sanitized route):
 *   POLISH_ROUTE=/pipelines POLISH_SLUG=pipelines-list pnpm exec playwright test polish-capture.spec.ts
 *
 *   # Against staging — no local web server; global-setup health-checks the
 *   # target, seeds data and logs in once, writing storageState-staging.json:
 *   E2E_TARGET=staging E2E_NO_WEBSERVER=true \
 *     E2E_ADMIN_EMAIL=<admin> E2E_ADMIN_PASSWORD=<password> \
 *     POLISH_ROUTE=/pipelines pnpm exec playwright test polish-capture.spec.ts
 *
 *   # (Same shape for prod: E2E_TARGET=app E2E_NO_WEBSERVER=true ...)
 *
 * ENV VARS
 *   POLISH_ROUTE  (required) Route path to capture, e.g. `/pipelines`. Must
 *                              begin with `/`. UNSET ⇒ the whole file skips.
 *   POLISH_SLUG   (optional) Output subdirectory under `.../polish/`. Defaults
 *                              to the route sanitized the same way
 *                              mobile-layout.spec.ts's `sanitizePath` does it
 *                              (`/pipelines` → `pipelines`).
 *
 * TARGET SELECTION — the real mechanism (tests/e2e/setup/env.ts)
 *   `getTarget()` reads `process.env.E2E_TARGET` and lowercases it; valid
 *   values are `local` (default), `staging`, `app`. `getBaseUrl()` honours
 *   `E2E_BASE_URL` when set, else the per-target default
 *   (local `http://127.0.0.1:5173`, staging `https://staging.modulo.run`,
 *   app `https://app.modulo.run`). Login uses the fixtures' `loginAsAdmin`
 *   with `E2E_ADMIN_EMAIL` / `E2E_ADMIN_PASSWORD` on non-local targets
 *   (`E2E_ORG_SLUG` selects the org on multi-org instances); non-local
 *   targets additionally reuse the `storageState-staging.json` written by
 *   `tests/e2e/setup/global-setup.ts`.
 *
 * OUTPUT — `frontend/tests/e2e/.mobile-captures/polish/<slug>/`
 *   (the parent `.mobile-captures/` is gitignored via frontend/.gitignore —
 *   captures are NEVER committed):
 *     desktop.png / desktop-full.png            1280 x 800
 *     desktop-wide.png / desktop-wide-full.png  1440 x 800
 *     mobile.png / mobile-full.png               390 x 844
 *     measurements.json   per viewport: scrollWidth / clientWidth / innerWidth
 *                         + computed `overflow`, the `main` element rect,
 *                         interactive-control count + bounding boxes, `clipped`
 *                         and `smallTargets` (with non-rendered/off-canvas
 *                         subtrees excluded), a `pageHealth` probe of any
 *                         rendered `error-alert-*` control (`{ healthy, signals
 *                         }` — recorded data the skill acts on, never an
 *                         assertion), plus the console errors collected during
 *                         the run.
 *
 * Full-page screenshots are best-effort per viewport: canvas/chart-heavy routes
 * (e.g. the pipeline editor) can crash a Playwright worker on a full-page
 * capture, so that shot is wrapped in try/catch and a failure is recorded in
 * `measurements.json` instead of aborting the run.
 *
 * This header is the authoritative source for this spec's own interface — the
 * polish skill's capture section should point here rather than copying a
 * command line.
 */
import fs from 'node:fs'
import path from 'node:path'
import type { Page, Response } from '@playwright/test'
import { test, expect, loginAsAdmin, setupLocalMockApi } from './setup/fixtures'
import { getTarget, getBaseUrl, type TestEnv } from './setup/env'

// Capture runs are single-shot artefacts — retrying on flake is noise. Top-level
// scope applies to every test in this file (same rationale as mobile-layout).
test.describe.configure({ retries: 0 })

const POLISH_ROUTE = process.env.POLISH_ROUTE ?? ''
const POLISH_SLUG = process.env.POLISH_SLUG?.trim() ?? ''

// Same rule as mobile-layout.spec.ts's sanitizePath (not imported from there —
// specs stay independent); keeps the slug filesystem-safe.
function sanitizePath(p: string): string {
  return p.replace(/[^a-z0-9-]/gi, '_').replace(/^_+|_+$/g, '') || 'root'
}

const SLUG = POLISH_SLUG || sanitizePath(POLISH_ROUTE)

// The three viewports the polish criteria name, in capture order.
const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'desktop-wide', width: 1440, height: 800 },
  { name: 'mobile', width: 390, height: 844 },
] as const

type ViewportName = (typeof VIEWPORTS)[number]['name']

// Interactive-control selector — same scan as mobile-layout's clipping check.
const INTERACTIVE_SELECTOR = 'button, a, input, select, textarea, [tabindex], [role="button"]'

// Small-target threshold in CSS px (WCAG 2.5.5 / 2.5.8 target size).
const SMALL_TARGET_MIN_PX = 44

// Responsive layout needs a beat after setViewportSize before measuring —
// media-query reflows and JS layout listeners run asynchronously.
const REFLOW_SETTLE_MS = 500

// Bounded app-mount / content-settle waits (mirrors mobile-layout): a dead
// target must fail fast with a legible error, not hang for the test timeout.
const APP_MOUNT_TIMEOUT_MS = 30_000
const CONTENT_SETTLE_TIMEOUT_MS = 15_000

// Mirror the fixtures.ts mock token values (not exported there) so the local
// auth seed keeps the app's session markers consistent across specs.
const MOCK_ACCESS_TOKEN = 'mock-access-token-for-e2e-tests'
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

interface ConsoleEntry {
  segment: string
  type: 'console.error' | 'pageerror'
  text: string
}

/**
 * Collect console errors + uncaught page errors for the whole run, tagged with
 * the capture segment (navigation, then each viewport) they occurred in.
 * Advisory only — never fails the test.
 */
function attachConsoleCollector(page: Page, currentSegment: () => string): ConsoleEntry[] {
  const entries: ConsoleEntry[] = []
  const push = (type: ConsoleEntry['type'], text: string) => {
    if (entries.length >= 200) return
    entries.push({ segment: currentSegment(), type, text: text.slice(0, 500) })
  }
  page.on('console', (msg) => {
    if (msg.type() === 'error') push('console.error', msg.text())
  })
  page.on('pageerror', (err) => push('pageerror', err.message))
  return entries
}

interface Box {
  tag: string
  cls: string
  text: string
  x: number
  y: number
  width: number
  height: number
}

interface ClipEntry {
  tag: string
  cls: string
  text: string
  left: number
  right: number
}

interface SmallTargetEntry extends Box {
  measuredVia: 'self' | 'label'
}

/**
 * Rendered-error probe. `healthy` is true when no `error-alert-*` control is in
 * the DOM. Recorded DATA only — the consuming polish skill acts on it; the test
 * never asserts it (an error-state render is a valid capture artefact).
 */
interface PageHealth {
  healthy: boolean
  signals: string[]
}

interface ViewportMeasurement {
  viewport: ViewportName
  viewportWidth: number
  viewportHeight: number
  scrollWidth: number
  clientWidth: number
  innerWidth: number
  overflow: boolean
  main: { x: number; y: number; width: number; height: number } | null
  controls: number
  renderedControls: number
  sampled: boolean
  controlBoxes: Box[]
  clipped: ClipEntry[]
  smallTargets: SmallTargetEntry[]
  pageHealth: PageHealth
  screenshots: { viewport: string; fullPage: string | null; fullPageError: string | null }
  consoleErrors: { type: ConsoleEntry['type']; text: string }[]
}

interface MeasureArgs {
  selector: string
  viewportName: ViewportName
  viewportWidth: number
  viewportHeight: number
  smallTargetMin: number
}

// Wait for the Vue app to mount, failing fast with the HTTP status whenever the
// target serves an error/non-SPA page instead of the app shell.
async function waitForAppMount(page: Page, route: string, response: Response | null): Promise<void> {
  if (response && response.status() >= 500) {
    throw new Error(
      `[polish-capture] ${route} returned HTTP ${response.status()} (${page.url()}) — ` +
        'the target is not serving the SPA; the capture would be garbage.',
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
      `[polish-capture] ${route} did not mount within ${APP_MOUNT_TIMEOUT_MS}ms (${page.url()}) — ` +
        'the target returned an error/non-SPA page; the capture would be garbage.',
    )
  }
}

async function settle(page: Page): Promise<void> {
  // The data-loading marker is not universal — only AnalyticsView and
  // SettingsLicenseView render it — so a blanket waitForSelector on any other
  // route never succeeded and, swallowed, made a route stuck mid-load
  // indistinguishable from a settled one. Wait only when a marker is actually
  // present, and let a present-but-stuck marker throw: a swallowed settle here
  // would surface later as a misleading capture assertion.
  //
  // A single waitForFunction (rather than a count check followed by a
  // waitForSelector) closes the race where a lazily route-mounted view renders
  // its [data-loading] root just after the count reads 0 — the predicate is
  // re-evaluated until no marker remains or every marker reports "false".
  await page.waitForFunction(
    () =>
      Array.from(document.querySelectorAll('[data-loading]')).every(
        (el) => el.getAttribute('data-loading') === 'false',
      ),
    undefined,
    { timeout: CONTENT_SETTLE_TIMEOUT_MS },
  )
}

/**
 * Authenticate and navigate to POLISH_ROUTE (mirrors mobile-layout's
 * preparePage). Throws when the capture would land on the wrong page (login
 * redirect, auth-guard bounce) — a silently-wrong capture is exactly the
 * failure mode this tool must not produce.
 */
async function preparePage(page: Page, env: TestEnv): Promise<string> {
  const route = POLISH_ROUTE
  const response = await page.goto(route)
  await waitForAppMount(page, route, response)
  await settle(page)

  if (env.name === 'local') {
    // Local: mock all /api/v1 traffic and seed the auth tokens so
    // authenticated routes render with mocked data (mirrors loginAsAdmin's
    // local branch). Re-navigate so the router guard picks up the seeded
    // session; /login stays untouched so the login page renders as-is —
    // seeding a session there would bounce it to the dashboard.
    await setupLocalMockApi(page)
    if (route !== '/login') {
      await page.evaluate(([token, refresh]) => {
        localStorage.setItem('modulo_access_token', token)
        localStorage.setItem('modulo_refresh_token', refresh)
      }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN])
      const remount = await page.goto(route)
      await waitForAppMount(page, route, remount)
      await settle(page)
    }
  } else if (page.url().includes('/login') && route !== '/login') {
    // storageState may have expired — fall back to a single real login, then
    // retry the route and re-run the mount/settle waits.
    await loginAsAdmin(page, env)
    const retry = await page.goto(route)
    await waitForAppMount(page, route, retry)
    await settle(page)
  }

  const finalUrl = page.url()
  const redirectedToLogin = finalUrl.includes('/login') && route !== '/login'
  const differsFromPath = !finalUrl.includes(route)
  if (redirectedToLogin || differsFromPath) {
    throw new Error(
      `[polish-capture] refusing to capture ${route} — landed on ${finalUrl} ` +
        '(auth guard / redirect). Capturing the wrong page would silently corrupt the polish pass.',
    )
  }
  return finalUrl
}

test.describe('polish capture', () => {
  // INERT IN CI: no POLISH_ROUTE ⇒ skip the whole file. A capture tool must
  // never fail or slow the normal e2e run.
  test.skip(
    !POLISH_ROUTE,
    'POLISH_ROUTE is not set — the polish capture spec is opt-in (see the header for the exact invocation)',
  )

  test(`capture ${POLISH_ROUTE} at three viewports`, async ({ page, env }) => {
    // Generous budget: three viewport reflows + six screenshots + a full DOM
    // measure per viewport. (Playwright's TestDetails does not carry `timeout`
    // — only tag/annotation/lock — so the budget is set in-body.)
    test.setTimeout(120_000)
    expect(
      POLISH_ROUTE.startsWith('/'),
      `POLISH_ROUTE must be a route path beginning with "/": got "${POLISH_ROUTE}"`,
    ).toBe(true)

    const outDir = path.join(__dirname, '.mobile-captures', 'polish', SLUG)
    fs.mkdirSync(outDir, { recursive: true })

    let segment = 'navigation'
    const consoleEntries = attachConsoleCollector(page, () => segment)

    const finalUrl = await preparePage(page, env)
    process.stdout.write(`[polish-capture] capturing ${POLISH_ROUTE} → ${outDir} (target=${getTarget()})\n`)

    const viewports: Record<string, ViewportMeasurement> = {}

    for (const vp of VIEWPORTS) {
      segment = vp.name
      await page.setViewportSize({ width: vp.width, height: vp.height })
      // Let the responsive layout reflow before measuring/screenshotting.
      await page.waitForTimeout(REFLOW_SETTLE_MS)

      // Measure first (rects are viewport-relative — capture them before the
      // screenshot machinery scrolls the page).
      const measured = await page.evaluate(
        ({ selector, viewportName, viewportWidth, viewportHeight, smallTargetMin }: MeasureArgs) => {
          const doc = document.documentElement
          const vw = doc.clientWidth
          const round = (n: number): number => Math.round(n * 10) / 10

          function clsOf(el: Element): string {
            return typeof el.className === 'string' ? el.className.slice(0, 80) : ''
          }
          function textOf(el: Element): string {
            return (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 60)
          }
          function toBox(el: Element, rect: DOMRect): Box {
            return {
              tag: el.tagName.toLowerCase(),
              cls: clsOf(el),
              text: textOf(el),
              x: round(rect.left),
              y: round(rect.top),
              width: round(rect.width),
              height: round(rect.height),
            }
          }

          // MANDATORY exclusion: an element inside a non-rendered / off-canvas
          // subtree (aria-hidden="true", inert, visibility:hidden,
          // display:none — on the element or ANY ancestor) is not rendered,
          // so it can never be genuinely "clipped". The naive version counts
          // the closed mobile navigation drawer (aside[aria-hidden][inert]
          // visibility:hidden, translated off-canvas) as ~10 clipped controls
          // — a false positive verified in a dry run.
          function isNonRendered(el: Element): boolean {
            let node: Element | null = el
            let depth = 0
            while (node && depth < 30) {
              if (node.getAttribute('aria-hidden') === 'true') return true
              if (node.hasAttribute('inert')) return true
              const cs = getComputedStyle(node)
              if (cs.visibility === 'hidden' || cs.display === 'none') return true
              node = node.parentElement
              depth += 1
            }
            return false
          }

          function findLabel(el: Element): Element | null {
            const wrapped = el.closest('label')
            if (wrapped) return wrapped
            if (el.id) {
              const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
              if (byFor) return byFor
            }
            const labelledby = el.getAttribute('aria-labelledby')
            if (labelledby) {
              const refId = labelledby.split(/\s+/)[0]
              const ref = refId ? document.getElementById(refId) : null
              if (ref) return ref
            }
            return null
          }

          // Effective target: a visually-hidden control (sr-only / 1×1) is
          // measured through its labelling <label> instead — a theme toggle's
          // real target is its 36×20 label, not the 1×1 hidden input (false
          // positive verified in a dry run).
          function effectiveTarget(el: Element): { rect: DOMRect; measuredVia: 'self' | 'label' } {
            const rect = el.getBoundingClientRect()
            const visuallyHidden =
              rect.width <= 1 ||
              rect.height <= 1 ||
              (el instanceof HTMLInputElement && /(^|\s)sr-only(\s|$)/.test(el.className))
            if (visuallyHidden) {
              const label = findLabel(el)
              if (label) {
                const lr = label.getBoundingClientRect()
                if (lr.width > 0 && lr.height > 0) return { rect: lr, measuredVia: 'label' }
              }
            }
            return { rect, measuredVia: 'self' }
          }

          const interactives = Array.from(document.querySelectorAll(selector))
          const clipped: ClipEntry[] = []
          const smallTargets: SmallTargetEntry[] = []
          const controlBoxes: Box[] = []
          let renderedControls = 0
          const sampled = interactives.length > 500

          for (const el of interactives.slice(0, 500)) {
            if (isNonRendered(el)) continue
            const rect = el.getBoundingClientRect()
            if (rect.width <= 0 || rect.height <= 0) continue
            renderedControls += 1
            controlBoxes.push(toBox(el, rect))

            if (rect.left < -1 || rect.right > vw + 1) {
              clipped.push({
                tag: el.tagName.toLowerCase(),
                cls: clsOf(el),
                text: textOf(el),
                left: round(rect.left),
                right: round(rect.right),
              })
            }

            const eff = effectiveTarget(el)
            if (eff.rect.width < smallTargetMin || eff.rect.height < smallTargetMin) {
              smallTargets.push({ ...toBox(el, eff.rect), measuredVia: eff.measuredVia })
            }
          }

          const mainEl = document.querySelector('main') ?? document.querySelector('[role="main"]')
          const mainRect = mainEl
            ? (() => {
                const r = mainEl.getBoundingClientRect()
                return { x: round(r.x), y: round(r.y), width: round(r.width), height: round(r.height) }
              })()
            : null

          return {
            viewport: viewportName,
            viewportWidth,
            viewportHeight,
            scrollWidth: doc.scrollWidth,
            clientWidth: vw,
            innerWidth: window.innerWidth,
            overflow: doc.scrollWidth > vw + 1,
            main: mainRect,
            controls: interactives.length,
            renderedControls,
            sampled,
            controlBoxes: controlBoxes.slice(0, 300),
            clipped: clipped.slice(0, 50),
            smallTargets: smallTargets.slice(0, 50),
          }
        },
        {
          selector: INTERACTIVE_SELECTOR,
          viewportName: vp.name,
          viewportWidth: vp.width,
          viewportHeight: vp.height,
          smallTargetMin: SMALL_TARGET_MIN_PX,
        },
      )

      // Rendered-error probe: record a signal for each shared ErrorAlert
      // control in the DOM (root has no testid; its buttons do). Recorded, not
      // asserted — a capture of an error state is still a successful capture,
      // but the consuming skill must be able to SEE it.
      const pageHealth: PageHealth = await page.evaluate(() => {
        const alerts = Array.from(document.querySelectorAll('[data-testid^="error-alert-"]'))
        const signals = alerts.map((el) => {
          const text = (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 80)
          return text || (el.getAttribute('data-testid') ?? 'error-alert')
        })
        return { healthy: signals.length === 0, signals }
      })

      // Screenshots: viewport shot (required — its failure fails the capture),
      // full-page shot (best-effort — canvas/chart-heavy routes can crash the
      // Playwright worker, so a failure is recorded, never fatal).
      const viewportFile = `${vp.name}.png`
      const fullFile = `${vp.name}-full.png`
      await page.screenshot({ path: path.join(outDir, viewportFile) })
      let fullPage: string | null = fullFile
      let fullPageError: string | null = null
      try {
        await page.screenshot({ path: path.join(outDir, fullFile), fullPage: true })
      } catch (err) {
        fullPage = null
        fullPageError = err instanceof Error ? err.message : String(err)
        process.stdout.write(
          `[polish-capture] full-page screenshot failed at ${vp.name} (${vp.width}x${vp.height}): ${fullPageError}\n`,
        )
      }

      const measurement: ViewportMeasurement = {
        ...measured,
        pageHealth,
        screenshots: { viewport: viewportFile, fullPage, fullPageError },
        consoleErrors: consoleEntries
          .filter((e) => e.segment === vp.name)
          .map(({ type, text }) => ({ type, text })),
      }
      viewports[vp.name] = measurement

      process.stdout.write(
        `[polish-capture] ${vp.name} ${vp.width}x${vp.height}: ` +
          `overflow=${measurement.overflow} (scrollWidth=${measurement.scrollWidth} clientWidth=${measurement.clientWidth} ` +
          `innerWidth=${measurement.innerWidth}), controls=${measurement.controls} ` +
          `clipped=${measurement.clipped.length}, smallTargets=${measurement.smallTargets.length}\n`,
      )
    }

    const payload = {
      route: POLISH_ROUTE,
      slug: SLUG,
      target: getTarget(),
      baseUrl: getBaseUrl(),
      finalUrl,
      capturedAt: new Date().toISOString(),
      viewports,
      consoleErrors: consoleEntries.map(({ segment: s, type, text }) => ({ segment: s, type, text })),
    }
    const jsonPath = path.join(outDir, 'measurements.json')
    fs.writeFileSync(jsonPath, `${JSON.stringify(payload, null, 2)}\n`)

    // Capture-success assertions only — files written, measurements produced.
    // Visual correctness is adjudicated downstream by the polish skill.
    for (const vp of VIEWPORTS) {
      const shotPath = path.join(outDir, `${vp.name}.png`)
      expect(fs.existsSync(shotPath), `viewport screenshot missing: ${shotPath}`).toBe(true)
      const m = viewports[vp.name]
      expect(m, `no measurement recorded for ${vp.name}`).toBeTruthy()
      expect(typeof m.scrollWidth, `scrollWidth not numeric for ${vp.name}`).toBe('number')
      expect(typeof m.overflow, `overflow not boolean for ${vp.name}`).toBe('boolean')
      if (m.screenshots.fullPage) {
        const fullShotPath = path.join(outDir, m.screenshots.fullPage)
        expect(fs.existsSync(fullShotPath), `full-page screenshot missing: ${fullShotPath}`).toBe(true)
      }
    }
    expect(fs.existsSync(jsonPath), `measurements.json missing: ${jsonPath}`).toBe(true)
    const parsed = JSON.parse(fs.readFileSync(jsonPath, 'utf-8')) as {
      route: string
      viewports: Record<string, ViewportMeasurement>
    }
    expect(parsed.route).toBe(POLISH_ROUTE)
    expect(Object.keys(parsed.viewports)).toEqual(VIEWPORTS.map((vp) => vp.name))
    process.stdout.write(`[polish-capture] wrote ${jsonPath}\n`)
  })
})
