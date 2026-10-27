import { test, loginAsAdmin } from './setup/fixtures'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * PrimeVue visual-baseline capture (FAR-2205).
 *
 * This is a MEASUREMENT harness, not a pass/fail gate. It captures, for a
 * fixed route set at fixed viewports:
 *
 *   1. a full-page screenshot,
 *   2. the computed `--p-*` design tokens on the document root, and
 *   3. a computed-style fingerprint of every PrimeVue component root on the
 *      page (font-size, padding, height, colours, radii, shadows).
 *
 * The capture is written to `PRIMEVUE_BASELINE_OUT` (default
 * `test-results/primevue-baseline`) so the same script can produce a v5
 * baseline and a v4 capture, which `scripts/compare-primevue-baseline.mjs`
 * then diffs. The artefacts are deliberately outside the assertion surface:
 * a drifted screenshot must never fail a normal test run — it must produce a
 * diff a human reviews.
 *
 * Local-only: the mock API (`loginAsAdmin`) is deterministic, whereas a real
 * deployment's data is not, so a capture against staging/app would not be a
 * comparable baseline.
 */

const OUT_DIR = process.env.PRIMEVUE_BASELINE_OUT || 'test-results/primevue-baseline'

interface RouteCapture {
  slug: string
  path: string
  /** Wait for a selector that proves the view rendered (not the dashboard fallback). */
  ready: string
  /** Optional per-route API fixtures registered before navigation. */
  mock?: (page: import('@playwright/test').Page) => Promise<void>
}

const EDITOR_PIPELINE_ID = 'e2e-editor-pipeline'

/**
 * The pipeline editor is the one route whose generic mock envelope
 * (`{items: [], total: 0}`) would render its "version missing" state instead
 * of the toolbar + canvas, so pin the two GETs it needs (mirrors
 * pipeline-editor.spec.ts).
 */
async function mockEditorPipeline(page: import('@playwright/test').Page): Promise<void> {
  await page.route(`**/api/v1/pipelines/${EDITOR_PIPELINE_ID}`, (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: EDITOR_PIPELINE_ID,
          name: 'E2E Editor Pipeline',
          organisation_id: '1',
          description: 'Pipeline used by the visual baseline',
          visibility: 'org',
          status: 'idle',
          created_at: '2026-01-01T10:00:00Z',
          updated_at: '2026-01-01T10:00:00Z',
          archived_at: null,
        }),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await page.route(`**/api/v1/pipelines/${EDITOR_PIPELINE_ID}/graph`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nodes: [{ id: 'node-1', node_type: 'agent', label: 'Reviewer Agent', position: { x: 100, y: 100 } }],
        edges: [],
      }),
    }),
  )
}

const DESKTOP = { width: 1280, height: 800 }
const MOBILE = { width: 390, height: 844 }

const ROUTES: RouteCapture[] = [
  { slug: 'dashboard', path: '/', ready: 'h1' },
  { slug: 'pipelines', path: '/pipelines', ready: 'h1' },
  {
    slug: 'pipeline-editor',
    path: `/pipelines/${EDITOR_PIPELINE_ID}/editor`,
    ready: '[data-testid="pipeline-editor-root"]',
    mock: mockEditorPipeline,
  },
  { slug: 'runs', path: '/runs', ready: 'h1' },
  { slug: 'run-detail', path: '/runs/101', ready: 'h1' },
  { slug: 'settings-license', path: '/settings/license', ready: 'h1' },
  { slug: 'admin-users', path: '/admin/users', ready: 'h1' },
  { slug: 'admin-connectors', path: '/admin/connectors', ready: 'h1' },
  { slug: 'admin-model-backends', path: '/admin/model-backends', ready: 'h1' },
  { slug: 'schemas', path: '/schemas', ready: 'h1' },
  { slug: 'settings-triggers', path: '/settings/triggers', ready: 'h1' },
  { slug: 'admin-run-retention', path: '/admin/run-retention', ready: 'h1' },
  { slug: 'settings-mcp', path: '/settings/mcp', ready: 'h1' },
]

const MOBILE_SLUGS = ['dashboard', 'pipelines', 'runs']

/**
 * Interactive-state captures. A closed dialog / select / menu is invisible to
 * a route screenshot, yet those are exactly the components whose Aura preset
 * changed most between v4 and v5 (dialog title size, menu item padding, select
 * dropdown width). Each entry drives the real UI into the state, then captures
 * it with the same token + fingerprint pipeline as a route.
 */
interface InteractionCapture {
  slug: string
  path: string
  /** Selector that must be visible before interacting. */
  ready: string
  /** Drive the UI into the state under test. */
  interact: (page: import('@playwright/test').Page) => Promise<void>
  /** Selector that proves the interactive state is open. */
  open: string
}

const INTERACTIONS: InteractionCapture[] = [
  {
    slug: 'pipelines-row-menu',
    path: '/pipelines',
    ready: 'h1',
    interact: async (page) => {
      // The popup Menu is toggled from a row handler; a click issued while the
      // table is still patching can be swallowed, so retry until the overlay
      // actually appears (bounded).
      const trigger = page.locator('[data-testid="pipeline-list-action-menu"]').first()
      for (let attempt = 0; attempt < 3; attempt++) {
        await trigger.click()
        if (await page.locator('.p-menu').first().isVisible().catch(() => false)) return
        await page.waitForTimeout(500)
      }
    },
    open: '.p-menu',
  },
  {
    slug: 'pipelines-folder-dialog',
    path: '/pipelines',
    ready: 'h1',
    interact: async (page) => {
      await page.locator('[data-testid="folder-tree-new"]').first().click()
    },
    open: '.p-dialog',
  },
  {
    slug: 'runs-select-open',
    path: '/runs',
    ready: 'h1',
    interact: async (page) => {
      await page.locator('.p-select').first().click()
    },
    open: '.p-select-overlay',
  },
  {
    slug: 'settings-license-dialog',
    path: '/settings/license',
    ready: 'h1',
    interact: async (page) => {
      await page.locator('[data-testid="license-key-input"]').fill('MC-TEST-LICENSE-KEY')
      await page.locator('[data-testid="license-apply-btn"]').click()
    },
    open: '.p-dialog',
  },
  {
    slug: 'parameter-schemas-tabs',
    path: '/admin/parameter-schemas',
    ready: 'h1',
    interact: async (page) => {
      await page.locator('[data-testid="paramschema-new"]').click()
    },
    open: '.p-tabs',
  },
  {
    slug: 'dashboard-tooltip',
    path: '/',
    ready: 'h1',
    interact: async (page) => {
      // The recent-runs pipeline name carries the v-tooltip directive; hovering
      // it is the only way a tooltip (security/popover class) appears on a
      // default route.
      await page.locator('[data-testid="dashboard-recent-runs"]').first().waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {})
      await page.locator('text=Release Pipeline').first().hover()
    },
    open: '.p-tooltip',
  },
  {
    slug: 'admin-users-dialog',
    path: '/admin/users',
    ready: 'h1',
    interact: async (page) => {
      await page.locator('[data-testid="admin-users-add-user"]').click()
    },
    open: '.p-dialog',
  },
]

/**
 * Collect every `--p-*` custom property the page's stylesheets declare (plus
 * any the token bridge wrote inline on <html>) and resolve its value.
 */
function collectPrimeTokens(): Record<string, string> {
  const names = new Set<string>()

  const walk = (rules: CSSRuleList): void => {
    for (const rule of Array.from(rules)) {
      const style = (rule as CSSStyleRule).style
      if (style) {
        for (let i = 0; i < style.length; i++) {
          const name = style[i]
          if (name.startsWith('--p-')) names.add(name)
        }
      }
      const nested = (rule as CSSGroupingRule).cssRules
      if (nested) walk(nested)
    }
  }

  for (const sheet of Array.from(document.styleSheets)) {
    try {
      walk(sheet.cssRules)
    } catch {
      // A cross-origin sheet would throw; none exist in this app.
    }
  }

  const inline = document.documentElement.style
  for (let i = 0; i < inline.length; i++) {
    const name = inline[i]
    if (name.startsWith('--p-')) names.add(name)
  }

  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of Array.from(names).sort()) {
    out[name] = computed.getPropertyValue(name).trim()
  }
  return out
}

/**
 * The router gates permission-declaring routes (`/settings/license` declares
 * `org.license.manage`) on the caller-scoped `GET /api/v1/auth/permissions`
 * payload. The shared catch-all mock answers it with an empty list-envelope,
 * so without this grant the route redirects to the dashboard and the capture
 * silently duplicates it. Registered AFTER `loginAsAdmin` so it wins over the
 * catch-all.
 */
async function mockPermissions(page: import('@playwright/test').Page): Promise<void> {
  await page.route('**/api/v1/auth/permissions*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ permissions: ['org.license.manage', 'analytics.query'] }),
    }),
  )
}

/** Component roots whose *rendered* appearance we fingerprint. */
const COMPONENT_SELECTORS = [
  '.p-button',
  '.p-inputtext',
  '.p-dialog',
  '.p-card',
  '.p-badge',
  '.p-menu',
  '.p-select',
  '.p-tabs',
  '.p-tab',
  '.p-popover',
  '.p-tooltip',
] as const

const FINGERPRINT_PROPS = [
  'display',
  'position',
  'font-family',
  'font-size',
  'font-weight',
  'line-height',
  'letter-spacing',
  'color',
  'background-color',
  'border-top-width',
  'border-top-style',
  'border-top-color',
  'border-radius',
  'padding-top',
  'padding-right',
  'padding-bottom',
  'padding-left',
  'margin-top',
  'margin-right',
  'margin-bottom',
  'margin-left',
  'width',
  'height',
  'gap',
  'box-shadow',
] as const

/**
 * A computed-style fingerprint for the first few instances of each PrimeVue
 * component root. Bounded per selector so a long list page does not produce a
 * multi-megabyte dump.
 */
function collectComponentFingerprints(args: {
  selectors: readonly string[]
  props: readonly string[]
}): Record<string, Array<Record<string, string>>> {
  const out: Record<string, Array<Record<string, string>>> = {}
  for (const selector of args.selectors) {
    const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 5)
    out[selector] = nodes.map((node) => {
      const cs = getComputedStyle(node)
      const record: Record<string, string> = {}
      for (const prop of args.props) record[prop] = cs.getPropertyValue(prop)
      record.__text = (node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80)
      return record
    })
  }
  return out
}

async function settle(page: import('@playwright/test').Page): Promise<void> {
  await page.waitForLoadState('networkidle')
  // Two frames so lazy charts/layout settle before the screenshot.
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
      }),
  )
}

test.describe('PrimeVue visual baseline capture', () => {
  test.beforeAll(() => {
    mkdirSync(OUT_DIR, { recursive: true })
  })

  for (const route of ROUTES) {
    test(`capture ${route.slug}`, async ({ page, env }) => {
      test.skip(env.name !== 'local', 'visual baseline capture is local-only (deterministic mock API)')

      await page.setViewportSize(DESKTOP)
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' })
      await loginAsAdmin(page, env)
      await mockPermissions(page)
      if (route.mock) await route.mock(page)

      await page.goto(route.path)
      await page.locator(route.ready).first().waitFor({ state: 'visible', timeout: 20_000 })
      await settle(page)

      const slug = `desktop-${route.slug}`

      await page.screenshot({
        path: join(OUT_DIR, `${slug}.png`),
        fullPage: true,
        animations: 'disabled',
        caret: 'hide',
      })

      const payload = {
        slug,
        path: route.path,
        viewport: DESKTOP,
        url: page.url(),
        title: await page.title(),
        tokens: await page.evaluate(collectPrimeTokens),
        components: await page.evaluate(collectComponentFingerprints, { selectors: COMPONENT_SELECTORS, props: FINGERPRINT_PROPS }),
      }
      writeFileSync(join(OUT_DIR, `${slug}.json`), JSON.stringify(payload, null, 1))
    })
  }

  for (const capture of INTERACTIONS) {
    test(`capture ${capture.slug}`, async ({ page, env }) => {
      test.skip(env.name !== 'local', 'visual baseline capture is local-only (deterministic mock API)')

      await page.setViewportSize(DESKTOP)
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' })
      await loginAsAdmin(page, env)
      await mockPermissions(page)

      await page.goto(capture.path)
      await page.locator(capture.ready).first().waitFor({ state: 'visible', timeout: 20_000 })
      await settle(page)

      await capture.interact(page)
      await page.locator(capture.open).first().waitFor({ state: 'visible', timeout: 10_000 })
      await settle(page)

      const slug = `desktop-${capture.slug}`
      await page.screenshot({
        path: join(OUT_DIR, `${slug}.png`),
        fullPage: false,
        animations: 'disabled',
        caret: 'hide',
      })

      const payload = {
        slug,
        path: capture.path,
        viewport: DESKTOP,
        url: page.url(),
        title: await page.title(),
        tokens: await page.evaluate(collectPrimeTokens),
        components: await page.evaluate(collectComponentFingerprints, {
          selectors: COMPONENT_SELECTORS,
          props: FINGERPRINT_PROPS,
        }),
      }
      writeFileSync(join(OUT_DIR, `${slug}.json`), JSON.stringify(payload, null, 1))
    })
  }

  for (const slug of MOBILE_SLUGS) {
    const route = ROUTES.find((r) => r.slug === slug)
    if (!route) throw new Error(`unknown mobile route ${slug}`)

    test(`capture mobile-${route.slug}`, async ({ page, env }) => {
      test.skip(env.name !== 'local', 'visual baseline capture is local-only (deterministic mock API)')

      await page.setViewportSize(MOBILE)
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' })
      await loginAsAdmin(page, env)
      await mockPermissions(page)
      if (route.mock) await route.mock(page)

      await page.goto(route.path)
      await page.locator(route.ready).first().waitFor({ state: 'visible', timeout: 20_000 })
      await settle(page)

      const mobileSlug = `mobile-${route.slug}`

      await page.screenshot({
        path: join(OUT_DIR, `${mobileSlug}.png`),
        fullPage: true,
        animations: 'disabled',
        caret: 'hide',
      })

      const payload = {
        slug: mobileSlug,
        path: route.path,
        viewport: MOBILE,
        url: page.url(),
        title: await page.title(),
        tokens: await page.evaluate(collectPrimeTokens),
        components: await page.evaluate(collectComponentFingerprints, { selectors: COMPONENT_SELECTORS, props: FINGERPRINT_PROPS }),
      }
      writeFileSync(join(OUT_DIR, `${mobileSlug}.json`), JSON.stringify(payload, null, 1))
    })
  }
})
