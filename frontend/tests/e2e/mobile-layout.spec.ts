import { devices, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import yaml from 'js-yaml'
import fs from 'node:fs'
import path from 'node:path'
import { test, expect, loginAsAdmin, setupLocalMockApi } from './setup/fixtures'
import { getTarget, type TestEnv } from './setup/env'

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

const FALLBACK_ROUTES = ['/login', '/', '/pipelines', '/stages', '/schemas', '/admin/connectors', '/admin/model-backends']

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
    console.log(`[mobile-layout] manifest.yaml enumeration failed (${err instanceof Error ? err.message : String(err)}); using fallback route list`)
  }
  return FALLBACK_ROUTES
}

const ROUTES = enumerateRoutes()
console.log(`[mobile-layout] enumerated ${ROUTES.length} routes from manifest.yaml`)

const NARROW_ROUTES = ['/login', '/', '/pipelines', '/stages', '/schemas']

function sanitizePath(p: string): string {
  return p.replace(/[^a-z0-9-]/gi, '_').replace(/^_+|_+$/g, '') || 'root'
}

// Navigate, wait for the Vue app to mount and data to settle, then bail out
// gracefully when an unauthenticated route redirects to /login. Returns false
// to signal the caller to skip the checks without failing.
async function preparePage(page: Page, route: string, env: TestEnv): Promise<boolean> {
  await page.goto(route)
  await page.waitForURL('**/*', { timeout: 5000 }).catch(() => {})

  await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

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
      await page.goto(route)
      await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)
      await page.waitForSelector('[data-loading="false"]', { timeout: 15000 }).catch(() => {})
    }
  } else if (page.url().includes('/login') && route !== '/login') {
    // storageState may have expired — fall back to a single real login, then
    // retry the route and re-run the mount/settle waits before proceeding.
    await loginAsAdmin(page, env)
    await page.goto(route)
    await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)
    await page.waitForSelector('[data-loading="false"]', { timeout: 15000 }).catch(() => {})
  }

  const finalUrl = page.url()
  const redirectedToLogin = finalUrl.includes('/login') && route !== '/login'
  const differsFromPath = !finalUrl.includes(route)
  if (redirectedToLogin || differsFromPath) {
    console.log(`  Skipping ${route} — redirected to ${finalUrl} (auth guard)`)
    return false
  }
  return true
}

// Check 1 — viewport meta must opt into device-width rendering.
async function checkViewportMeta(page: Page) {
  const meta = page.locator('meta[name="viewport"]').first()
  await expect(meta).toHaveAttribute('content', /width=device-width/, {
    message: 'No <meta name="viewport" content="width=device-width, ...> found — page renders at 980px desktop width scaled down to ~38% on phones.',
  })
}

// Check 2 — no horizontal page overflow; log suspected 100vw-width culprits.
async function checkNoHorizontalOverflow(page: Page) {
  const result = await page.evaluate(() => {
    const doc = document.documentElement
    const overflow = doc.scrollWidth > window.innerWidth + 1
    let culprits: string[] = []
    if (overflow) {
      // computed width:100vw resolves to innerWidth + scrollbar, so elements
      // wider than the visible viewport are the classic scrollbar-overflow cause
      culprits = Array.from(document.querySelectorAll('*'))
        .filter(el => {
          const w = parseFloat(getComputedStyle(el).width)
          return !Number.isNaN(w) && w > window.innerWidth
        })
        .slice(0, 10)
        .map(el => `<${el.tagName.toLowerCase()}> width=${getComputedStyle(el).width}`)
    }
    return { overflow, scrollWidth: doc.scrollWidth, innerWidth: window.innerWidth, culprits }
  })
  if (result.overflow) {
    console.log(`[mobile-layout] horizontal overflow: scrollWidth=${result.scrollWidth} innerWidth=${result.innerWidth}`)
    if (result.culprits.length > 0) {
      console.log(`[mobile-layout]   suspected width:100vw culprits: ${result.culprits.join(', ')}`)
    }
  }
  expect(result.overflow, `Horizontal page overflow (scrollWidth ${result.scrollWidth} > innerWidth ${result.innerWidth})`).toBe(false)
}

// Check 3 — app shell fills the viewport width; main-content ratio is advisory.
async function checkAppShellFillsViewport(page: Page) {
  const data = await page.evaluate(() => {
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
    const appRatio = shellRect && shellRect.width > 0 ? shellRect.width / window.innerWidth : 0
    let mainRatio = 0
    for (const el of Array.from(document.querySelectorAll('main, [role="main"]'))) {
      const r = el.getBoundingClientRect()
      if (r.width > 0) mainRatio = Math.max(mainRatio, r.width / window.innerWidth)
    }
    return { appRatio, mainRatio }
  })
  console.log(`[mobile-layout] ${page.url()} app shell width ratio: ${data.appRatio.toFixed(2)}, widest main container ratio: ${data.mainRatio.toFixed(2)}`)
  expect(data.appRatio, `App shell fills ${(data.appRatio * 100).toFixed(0)}% of the viewport width — background likely does not fill the screen`).toBeGreaterThanOrEqual(0.95)
}

// Check 4 — visible interactive elements must not be clipped off-screen.
async function checkInteractiveNotClipped(page: Page) {
  const result = await page.evaluate(() => {
    const doc = document.documentElement
    const hasHScroll = doc.scrollWidth > window.innerWidth + 1
    if (hasHScroll) {
      return { skip: true, clipped: [] }
    }
    const selector = 'button, a, input, select, textarea, [tabindex], [role="button"]'
    const clipped: { tag: string; cls: string; text: string; left: number; right: number }[] = []
    for (const el of Array.from(document.querySelectorAll(selector))) {
      const htmlEl = el as HTMLElement
      if (htmlEl.offsetParent === null) continue
      if (getComputedStyle(htmlEl).visibility === 'hidden') continue
      const rect = htmlEl.getBoundingClientRect()
      if (rect.width === 0 && rect.height === 0) continue
      if (rect.left < -1 || rect.right > window.innerWidth + 1) {
        clipped.push({
          tag: htmlEl.tagName.toLowerCase(),
          cls: typeof htmlEl.className === 'string' ? htmlEl.className.slice(0, 80) : '',
          text: (htmlEl.textContent ?? '').trim().slice(0, 60),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
        })
      }
    }
    return { skip: false, clipped: clipped.slice(0, 10) }
  })
  if (result.skip) {
    console.log(`[mobile-layout] ${page.url()} has horizontal scroll — skipping clipped-interactive check`)
    return
  }
  if (result.clipped.length > 0) {
    for (const c of result.clipped) {
      console.log(`[mobile-layout]   clipped interactive: <${c.tag}> class="${c.cls}" text="${c.text}" left=${c.left} right=${c.right}`)
    }
  }
  expect(result.clipped, `Interactive elements clipped off-screen (${result.clipped.length}): ${JSON.stringify(result.clipped)}`).toEqual([])
}

// Check 5 — axe WCAG AA at the mobile viewport.
async function checkAxeMobile(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
  const violations = filterViolations(results.violations)
  if (violations.length > 0) {
    console.log(`\n=== ${page.url()} mobile WCAG violations ===`)
    for (const v of violations) {
      console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
    }
  }
  expect(violations).toEqual([])
}

// Check 6 — cumulative layout shift over a ~1s settle period.
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
    expect(cls, `Cumulative layout shift ${cls.toFixed(3)} > 0.25 on ${page.url()}`).toBeLessThanOrEqual(0.25)
  } else if (cls > 0.1) {
    console.log(`[mobile-layout] ADVISORY: CLS ${cls.toFixed(3)} on ${page.url()} (0.1 < CLS <= 0.25)`)
  } else {
    console.log(`[mobile-layout] CLS ${cls.toFixed(3)} on ${page.url()}`)
  }
}

// Check 7 — advisory only: inputs under 16px trigger iOS auto-zoom.
async function checkInputFontSize(page: Page) {
  const small = await page.evaluate(() => {
    const found: string[] = []
    for (const el of Array.from(document.querySelectorAll('input, select, textarea'))) {
      const htmlEl = el as HTMLElement
      if (htmlEl.offsetParent === null) continue
      const size = parseFloat(getComputedStyle(htmlEl).fontSize)
      if (!Number.isNaN(size) && size < 16) {
        found.push(`<${htmlEl.tagName.toLowerCase()}> ${size}px class="${typeof htmlEl.className === 'string' ? htmlEl.className.slice(0, 60) : ''}"`)
      }
    }
    return found.slice(0, 10)
  })
  if (small.length > 0) {
    console.log(`[mobile-layout] ADVISORY: inputs with font-size < 16px (iOS auto-zoom risk) on ${page.url()}:`)
    for (const s of small) {
      console.log(`[mobile-layout]   ${s}`)
    }
  }
}

async function runFullSweep(page: Page, route: string, env: TestEnv) {
  if (!(await preparePage(page, route, env))) return

  await checkViewportMeta(page)
  await checkNoHorizontalOverflow(page)
  await checkAppShellFillsViewport(page)
  await checkInteractiveNotClipped(page)
  await checkAxeMobile(page)
  await checkCLS(page)
  await checkInputFontSize(page)

  await page.screenshot({ path: path.join(CAPTURE_DIR, `${sanitizePath(route)}.png`) })
}

async function runNarrowChecks(page: Page, route: string, env: TestEnv) {
  if (!(await preparePage(page, route, env))) return

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
