import type { Page } from '@playwright/test'
import { expect, loginAsAdmin, test } from './setup/fixtures'

const PIPELINE_ID = 'canvas-visual-pipeline'
const SNAPSHOT_ID = 'canvas-visual-historic'
const LATEST_SNAPSHOT_ID = 'canvas-visual-latest'

const PIPELINE = {
  id: PIPELINE_ID,
  name: 'Canvas visual pipeline',
  organisation_id: '1',
  description: 'Deterministic pipeline used by canvas visual snapshots',
  visibility: 'org',
  status: 'idle',
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
  archived_at: null,
}

const EMPTY_GRAPH = { nodes: [], edges: [] }

const DIAMOND_GRAPH = {
  nodes: [
    { id: 'manual', node_type: 'manual', label: 'Manual input', position: { x: 0, y: 0 } },
    { id: 'agent', node_type: 'agent', label: 'Agent', position: { x: 240, y: 0 } },
    { id: 'router', node_type: 'router', label: 'Router', position: { x: 480, y: 0 } },
    { id: 'hitl', node_type: 'hitl', label: 'Human review', position: { x: 720, y: 0 } },
    { id: 'dispatch', node_type: 'dispatch', label: 'Dispatch', position: { x: 720, y: 160 } },
    { id: 'connector', node_type: 'connector', label: 'Connector', position: { x: 960, y: 0 } },
  ],
  edges: [
    { id: 'edge-manual-agent', source_node_id: 'manual', target_node_id: 'agent', edge_type: 'normal' },
    { id: 'edge-agent-router', source_node_id: 'agent', target_node_id: 'router', edge_type: 'normal' },
    { id: 'edge-router-hitl', source_node_id: 'router', target_node_id: 'hitl', edge_type: 'normal' },
    { id: 'edge-router-dispatch', source_node_id: 'router', target_node_id: 'dispatch', edge_type: 'llm', routing_label: 'fast lane' },
    { id: 'edge-hitl-connector', source_node_id: 'hitl', target_node_id: 'connector', edge_type: 'loop', max_iterations: 3 },
    {
      id: 'edge-dispatch-connector',
      source_node_id: 'dispatch',
      target_node_id: 'connector',
      edge_type: 'normal',
      hitl_review_config: { label: 'Release approval' },
    },
  ],
}

const HISTORIC_GRAPH = {
  nodes: [
    { id: 'historic-agent', node_type: 'agent', label: 'Historic agent', position: { x: 0, y: 0 } },
    { id: 'historic-hitl', node_type: 'hitl', label: 'Historic review', position: { x: 240, y: 0 } },
  ],
  edges: [{ id: 'historic-edge', source_node_id: 'historic-agent', target_node_id: 'historic-hitl', edge_type: 'normal' }],
}

const SNAPSHOT = {
  id: SNAPSHOT_ID,
  pipeline_id: PIPELINE_ID,
  snapshot_version: 4,
  created_at: '2026-01-04T10:00:00Z',
  created_by: 'visual-test-user',
  tag: 'Historic design',
  graph_json: HISTORIC_GRAPH,
}

const LATEST_SNAPSHOT = {
  id: LATEST_SNAPSHOT_ID,
  pipeline_id: PIPELINE_ID,
  snapshot_version: 5,
  created_at: '2026-01-05T10:00:00Z',
  created_by: 'visual-test-user',
  tag: 'Latest design',
}

const DIFF = {
  snapshot_a: HISTORIC_GRAPH,
  snapshot_b: DIAMOND_GRAPH,
  nodes_added: [{ id: 'dispatch', label: 'Dispatch', node_type: 'dispatch' }],
  nodes_removed: [],
  nodes_modified: [{ node_id: 'historic-agent', changes: { label: { old: 'Historic agent', new: 'Agent' } } }],
  edges_added: [{ id: 'edge-dispatch-connector', source: 'dispatch', target: 'connector', edge_type: 'normal' }],
  edges_removed: [],
  edges_modified: [],
  semantic: {
    port_changes: [],
    impacted_nodes: ['connector'],
    breaking_changes: [{ severity: 'warn', reason: 'A downstream connector changed' }],
  },
}

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 390, height: 844 },
] as const
const THEMES = ['light', 'dark'] as const

async function mockPipeline(page: Page): Promise<void> {
  await page.route(`**/api/v1/pipelines/${PIPELINE_ID}`, (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
}

async function mockGraph(page: Page, graph: unknown): Promise<void> {
  await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/graph`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(graph) }),
  )
}

async function mockSnapshots(page: Page): Promise<void> {
  await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/snapshots**`, (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith(`/${SNAPSHOT_ID}`)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SNAPSHOT) })
    }
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [LATEST_SNAPSHOT, SNAPSHOT], total: 2 }),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) })
  })
  await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/snapshots/diff`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DIFF) }),
  )
}

/**
 * Wait until the canvas has genuinely stopped moving.
 *
 * VueFlow animates its `fitView` transform for a few hundred milliseconds after
 * the graph mounts, and the editor re-fits once the measured node sizes land.
 * A fixed sleep captured a mid-animation transform: the baselines differed by
 * ~2% of pixels in thin 1-2px edge/node-border strokes spread across the whole
 * canvas, which is a sub-pixel-scale shift, not a styling change. Poll the
 * transformation pane until consecutive samples are equal (bounded, so a
 * genuinely animated page cannot hang the capture) — the same stabilisation the
 * PrimeVue baseline capture uses.
 */
async function settleCanvas(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle')
  await page.evaluate(() => document.fonts?.ready ?? Promise.resolve())
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        // The whole EDITOR LAYOUT is the unstable quantity, not just the canvas
        // transform: the docked toolbar re-wraps when an async chip (version
        // chip, capability scope, folder breadcrumb) settles, which shortens the
        // canvas pane and shifts every node by ~40px while the transform string
        // itself is unchanged. A capture taken across that reflow is a genuine
        // layout difference, so the signature covers the editor root, the
        // toolbar, the canvas box and the VueFlow transform together.
        const rect = (selector: string): string => {
          const el = document.querySelector(selector)
          if (!el) return '-'
          const box = el.getBoundingClientRect()
          return `${Math.round(box.x)},${Math.round(box.y)},${Math.round(box.width)},${Math.round(box.height)}`
        }
        const sample = (): string =>
          [
            rect('[data-testid="pipeline-editor-root"]'),
            rect('[data-testid="pipeline-editor-toolbar"]'),
            rect('[data-testid="pipeline-editor-canvas"]'),
            rect('.vue-flow'),
            document.querySelector('.vue-flow__transformationpane')?.getAttribute('style') ?? '-',
          ].join('|')
        const start = Date.now()
        let last = ''
        let stable = 0
        let ticks = 0
        const tick = (): void => {
          ticks += 1
          const next = sample()
          if (next === last) stable += 1
          else {
            stable = 0
            last = next
          }
          // 6 consecutive equal samples (600ms) AND at least 500ms elapsed, so
          // a slow async chip that settles late cannot be captured mid-reflow.
          const settled = stable >= 6 && Date.now() - start >= 500
          if (settled || ticks >= 60) {
            // Two animation frames after stability, so the painted frame is the
            // stabilised one rather than the last pre-stable frame.
            requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
            return
          }
          setTimeout(tick, 100)
        }
        setTimeout(tick, 100)
      }),
  )
  await page.waitForTimeout(150)
}

/**
 * Pin the ambient chrome that sits above the editor.
 *
 * The editor's height is "whatever the viewport leaves", so a banner appearing
 * (or changing height) ABOVE it changes the canvas pane, the fit transform and
 * therefore every pixel of the capture. The local mock's catch-all answers these
 * endpoints with a list envelope, which left the onboarding banner ACTIVE
 * (`is_first_run` defaults true on a missing field) at a height that varied by
 * a few pixels between runs — enough to re-fit the whole graph. These stubs make
 * the chrome a fixed, explicit part of the fixture: no onboarding banner, no
 * analytics-consent prompt, no DB-capacity banner.
 */
async function mockAmbientChrome(page: Page): Promise<void> {
  await page.route('**/api/v1/onboarding/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ is_first_run: false, dismissed: true, progress_pct: 100, actions: [] }),
    }),
  )
  await page.route('**/api/v1/org/product-analytics', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ level: 'off', prompted: 'dismissed', prompt_eligible: false, instance_enabled: false }),
    }),
  )
  await page.route('**/api/v1/admin/db-capacity', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ mode: 'disabled', alert_level: 'ok', capacity_percent: null, used_bytes: 0, capacity_bytes: 0 }),
    }),
  )
}

async function preparePage(page: Page, env: Parameters<typeof loginAsAdmin>[1], viewport: (typeof VIEWPORTS)[number], theme: string): Promise<void> {
  await page.setViewportSize({ width: viewport.width, height: viewport.height })
  await page.emulateMedia({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce' })
  await page.addInitScript((selectedTheme) => {
    localStorage.setItem('assistant-panel-state', 'closed')
    document.documentElement.classList.toggle('dark', selectedTheme === 'dark')
    document.documentElement.classList.toggle('light', selectedTheme === 'light')
  }, theme)
  await loginAsAdmin(page, env)
  await mockAmbientChrome(page)
  await mockPipeline(page)
  await mockSnapshots(page)
}

async function capture(page: Page, state: string, viewport: string, theme: string): Promise<void> {
  await settleCanvas(page)
  await expect(page.locator('[data-testid="pipeline-editor-root"]')).toHaveScreenshot(
    `canvas/${state}-${viewport}-${theme}.png`,
    {
      animations: 'disabled',
      caret: 'hide',
      scale: 'css',
      // Sub-pixel text/border antialiasing is not stable to the byte in
      // headless Chromium, and the committed Linux baselines were captured on a
      // host whose font rasterisation differs slightly from the GitHub runner's
      // (same image dimensions, same layout — only glyph/border edges shift by a
      // sub-pixel). The observed residual is 0.05–0.15% of the capture (184–402
      // pixels, identical in both themes, which is the signature of a geometry
      // difference rather than a colour or content change); the previous 0.05%
      // ceiling sat inside that noise band and failed healthy runs. 0.3% clears
      // it with ~2x headroom while still catching a real layout, colour or
      // marker change, which moves 1–2% of the capture.
      maxDiffPixelRatio: 0.003,
    },
  )
}

test.describe('Pipeline canvas visual snapshots', { tag: '@visual' }, () => {
  test.setTimeout(90_000)

  for (const viewport of VIEWPORTS) {
    for (const theme of THEMES) {
      test(`empty graph and toolbar — ${viewport.name} ${theme}`, { tag: '@visual' }, async ({ page, env }) => {
        await preparePage(page, env, viewport, theme)
        await mockGraph(page, EMPTY_GRAPH)
        await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
        await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: 60_000 })
        await expect(page.getByText('no components in pipeline')).toBeVisible()
        await capture(page, 'empty', viewport.name, theme)
      })

      test(`six node types and edge markers — ${viewport.name} ${theme}`, { tag: '@visual' }, async ({ page, env }) => {
        await preparePage(page, env, viewport, theme)
        await mockGraph(page, DIAMOND_GRAPH)
        await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
        await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: 60_000 })
        await expect(page.getByTestId('pipeline-node-router')).toBeVisible()
        await expect(page.getByTestId('pipeline-node-hitl')).toBeVisible()
        await expect(page.locator('.vue-flow__node-dispatch')).toBeVisible()
        await expect(page.locator('.vue-flow__node-connector')).toBeVisible()
        await expect(page.locator('.vue-flow__edge-path').first()).toHaveAttribute('marker-end', /url\(['"]?#.+\)/)
        await capture(page, 'six-node-types', viewport.name, theme)
      })

      test(`historic banner and compare mode — ${viewport.name} ${theme}`, { tag: '@visual' }, async ({ page, env }) => {
        await preparePage(page, env, viewport, theme)
        await mockGraph(page, DIAMOND_GRAPH)
        await page.goto(`/pipelines/${PIPELINE_ID}/editor?snapshot=${SNAPSHOT_ID}`)
        await expect(page.getByTestId('pipeline-editor-historic-banner')).toBeVisible({ timeout: 60_000 })
        await capture(page, 'historic-banner', viewport.name, theme)
        await page.getByTestId('pipeline-editor-historic-compare').click()
        await expect(page.getByTestId('pipeline-compare')).toBeVisible()
        await expect(page.getByTestId('pipeline-compare-summary')).toBeVisible()
        await capture(page, 'compare', viewport.name, theme)
      })
    }
  }
})
