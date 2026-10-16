import type { Page } from '@playwright/test'
import { test, expect, loginAsAdmin } from './setup/fixtures'

// Structural e2e for /pipelines/:id/editor (FAR-638, batch A). The editor
// route previously rotted broken with zero coverage (FAR-616/629), so these
// specs pin the FIXED behaviour: the page loads for an existing pipeline id,
// the toolbar + canvas render, and the save flow reaches the graph PATCH.
// Mirrors json-viewer.spec.ts: mock the :id endpoints with a fake id and
// navigate directly — structural assertions only, no seeded data.
const PIPELINE_ID = 'e2e-editor-pipeline'

const PIPELINE = {
  id: PIPELINE_ID,
  name: 'E2E Editor Pipeline',
  organisation_id: '1',
  description: 'Pipeline used by the e2e editor spec',
  visibility: 'org',
  status: 'idle',
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
  archived_at: null,
}

const EMPTY_GRAPH = { nodes: [], edges: [] }

// Staging readiness budget for the editor's first paint. The page gates its
// toolbar (and the save button) on a catalog fetch (schemas/connectors/agents/
// parameter-schemas) that a staging @regression trace showed taking >5 s under
// load — the pipeline + graph GETs had already returned, but the default 5 s
// expect budget flaked the readiness assertion below. 30 s matches the sibling
// real-stack journey budget.
const EDITOR_READY_TIMEOUT = 30_000

// Mock the pipeline + graph GETs (structural specs only — no seeded data).
// One node so the properties panels can be opened (node/edge selection).
async function mockEditorWithNode(page: Page) {
  await page.route(`**/api/v1/pipelines/${PIPELINE_ID}`, (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/graph`, (route) => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nodes: [{ id: 'node-1', node_type: 'agent', label: 'Reviewer Agent', position: { x: 100, y: 100 } }],
        edges: [],
      }),
    })
  })
}

test.describe('Pipeline Editor', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server (plus Vue Flow) can exceed the 30s default before any
  // assertion runs. Mirrors json-viewer.spec.ts.
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // The Assistant floating panel (rendered where dev-mode is on) opens by default
    // and its fixed-position overlay intercepts clicks on interactive page
    // content. Force the panel closed before the app boots. Mirrors
    // json-viewer.spec.ts / view-modes-admin.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('renders toolbar and canvas for an existing pipeline', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/graph`, (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_GRAPH) })
    })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)

    await expect(page).toHaveURL(new RegExp(`/pipelines/${PIPELINE_ID}/editor`))
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    await expect(page.getByTestId('pipeline-editor-toolbar').locator('h2')).toContainText('E2E Editor Pipeline')

    // File group: save + save-as-template
    await expect(page.getByTestId('pipeline-editor-save')).toBeVisible()
    await expect(page.getByTestId('pipeline-editor-save-as-template')).toBeVisible()
    // Run group
    await expect(page.getByTestId('pipeline-editor-run')).toBeVisible()
    // Canvas tools group
    await expect(page.getByTestId('pipeline-editor-add-node')).toBeVisible()
    await expect(page.getByTestId('pipeline-editor-fit-view')).toBeVisible()

    // The Vue Flow canvas area renders (empty graph → empty-state overlay hint)
    await expect(page.locator('.vue-flow').first()).toBeVisible()
    await expect(page.getByText('no components in pipeline')).toBeVisible()
  })

  test('save flow reaches the graph PATCH endpoint', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    const graphPatches: string[] = []
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/graph`, (route) => {
      if (route.request().method() === 'PATCH') {
        graphPatches.push(route.request().url())
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_GRAPH) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_GRAPH) })
    })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-save')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    await page.getByTestId('pipeline-editor-save').click()

    await expect.poll(() => graphPatches.length, {
      message: 'Save must issue a PATCH to the pipeline graph endpoint',
    }).toBeGreaterThan(0)
    await expect(page.getByTestId('pipeline-editor-save-error')).toHaveCount(0)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible()
  })

  test('renders graph nodes from the pipeline graph', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/graph`, (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          nodes: [{ id: 'node-1', node_type: 'agent', label: 'Reviewer Agent', position: { x: 100, y: 100 } }],
          edges: [],
        }),
      })
    })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)

    await expect(page.locator('.vue-flow').first()).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    await expect(page.locator('.vue-flow__node').first()).toBeVisible()
    // With a node present the Run button is enabled (disabled only when the graph is empty)
    await expect(page.getByTestId('pipeline-editor-run')).toBeEnabled()
  })
})

// Mobile viewport behaviour (FAR-1841). Before the fix, at ~375x667 the editor
// was a fixed calc(100vh - 3.5rem) block sitting below the app chrome, so the
// canvas hung below the fold where VueFlow captures wheel/touch — nothing
// below the fold was reachable and pinch zoomed the graph instead of the page.
// Selecting a node then let the w-96 properties panel swallow the whole
// viewport, crushing the canvas to 0x0 with no way to deselect. These specs
// pin the fixed behaviour: the editor fits the leftover viewport height
// (toolbar + canvas fully on screen, no page scroll), and the properties
// panel opens as an overlay with a Close button that restores the canvas.
test.describe('Pipeline Editor mobile', { tag: '@regression' }, () => {
  test.setTimeout(90_000)
  test.use({ viewport: { width: 375, height: 667 }, hasTouch: true, isMobile: true })

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('editor fits the leftover viewport height — toolbar and canvas fully on screen', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    const flow = page.locator('.vue-flow').first()
    await expect(flow).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    const toolbarBox = await page.getByTestId('pipeline-editor-toolbar').boundingBox()
    const flowBox = await flow.boundingBox()
    expect(toolbarBox).not.toBeNull()
    expect(flowBox).not.toBeNull()

    // Toolbar is fully above the fold — Save / Run are reachable with no scroll.
    expect(toolbarBox!.y).toBeGreaterThanOrEqual(0)
    expect(toolbarBox!.y + toolbarBox!.height).toBeLessThanOrEqual(667)
    // The canvas has real width and ends at or above the viewport bottom —
    // nothing hangs below the fold behind the touch-capturing canvas.
    expect(flowBox!.width).toBeGreaterThan(200)
    expect(flowBox!.y + flowBox!.height).toBeLessThanOrEqual(667)

    // The document has no scrollable overflow while the editor is open.
    const doc = await page.evaluate(() => ({
      scrollHeight: document.documentElement.scrollHeight,
      clientHeight: document.documentElement.clientHeight,
    }))
    expect(doc.scrollHeight).toBeLessThanOrEqual(doc.clientHeight)
  })

  test('a short viewport yields the 240px floor instead of scrolling the page', { tag: '@regression' }, async ({ page, env }) => {
    // 640x480 (short landscape) is where the chrome stack above the editor
    // exceeds the 240px usability floor the original mobile fix introduced:
    // with a hard floor the editor was forced to 240px and spilled ~80px below
    // the fold, re-introducing the trapped page scroll FAR-1841 removed. The
    // floor must yield to the viewport (FAR-1861) — the editor shrinks to the
    // space left instead of overflowing.
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)
    await page.setViewportSize({ width: 640, height: 480 })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    const flow = page.locator('.vue-flow').first()
    await expect(flow).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    // The fit resolves async (chrome above the editor mounts/measures on its
    // own schedule) — poll until the document settles with no vertical scroll.
    await expect
      .poll(
        async () => page.evaluate(() => document.documentElement.scrollHeight - document.documentElement.clientHeight),
        { timeout: 15_000, message: 'short viewport must not scroll the page' },
      )
      .toBeLessThanOrEqual(0)

    const viewportHeight = await page.evaluate(() => document.documentElement.clientHeight)
    const rootBox = await page.getByTestId('pipeline-editor-root').boundingBox()
    expect(rootBox).not.toBeNull()
    // The 240px floor yielded: the editor is shorter than 240px here.
    expect(rootBox!.height).toBeLessThan(240)
    // …and ends at or above the fold, so nothing is trapped below it.
    expect(rootBox!.y + rootBox!.height).toBeLessThanOrEqual(viewportHeight + 1)
  })

  test('node properties panel opens as a closable overlay without crushing the canvas', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    const flow = page.locator('.vue-flow').first()
    await expect(page.locator('.vue-flow__node').first()).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    // Select the node — the properties panel must open with a Close button.
    await page.locator('.vue-flow__node').first().click()
    await expect(page.getByText('Node Properties')).toBeVisible()
    const closeBtn = page.getByTestId('pipeline-editor-node-props-close')
    await expect(closeBtn).toBeVisible()

    // The canvas and toolbar keep their layout (no w-96 sibling crush).
    const flowBox = await flow.boundingBox()
    const toolbarBox = await page.getByTestId('pipeline-editor-toolbar').boundingBox()
    expect(flowBox).not.toBeNull()
    expect(toolbarBox).not.toBeNull()
    expect(flowBox!.width).toBeGreaterThan(200)
    expect(toolbarBox!.width).toBeGreaterThan(200)

    // Close restores the editor: the panel unmounts, the canvas stays.
    await closeBtn.click()
    await expect(page.getByText('Node Properties')).toHaveCount(0)
    await expect(flow).toBeVisible()
    expect((await flow.boundingBox())!.width).toBeGreaterThan(200)
  })

  test('the mobile sheet is a labelled dialog and Escape closes it', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.locator('.vue-flow__node').first()).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    await page.locator('.vue-flow__node').first().click()

    const sheet = page.locator('aside', { has: page.getByTestId('pipeline-editor-node-props-close') })
    await expect(sheet).toBeVisible()
    // FAR-1861: the overlay sheet must announce itself as a labelled modal
    // dialog so AT/keyboard users are not left tabbing behind it.
    await expect(sheet).toHaveAttribute('role', 'dialog')
    await expect(sheet).toHaveAttribute('aria-modal', 'true')
    await expect(sheet).toHaveAttribute('aria-labelledby', 'pipeline-editor-node-sheet-title')

    // Focus moved into the sheet on open, so Escape lands on it and closes.
    await page.keyboard.press('Escape')
    await expect(page.getByText('Node Properties')).toHaveCount(0)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible()
  })

  test('fit observer re-fits on chrome above the editor, not on viewport style writes', { tag: '@regression' }, async ({ page, env }) => {
    // Count measurements of the editor root: refitMobileEditor's
    // getBoundingClientRect is the only page code that reads that element,
    // so each call == one scheduled re-fit of the mobile pin.
    await page.addInitScript(() => {
      const original = Element.prototype.getBoundingClientRect
      ;(window as any).__rootRectMeasures = 0
      Element.prototype.getBoundingClientRect = function (this: Element) {
        if (this.getAttribute('data-testid') === 'pipeline-editor-root') {
          ;(window as any).__rootRectMeasures += 1
        }
        return original.call(this)
      }
    })

    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)
    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    const flow = page.locator('.vue-flow').first()
    await expect(flow).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    const root = page.getByTestId('pipeline-editor-root')
    const measures = () => page.evaluate(() => (window as any).__rootRectMeasures as number)
    // Flush any re-fit already scheduled in a rAF before sampling the counter.
    const flushRefits = () =>
      page.evaluate(
        () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
      )

    await flushRefits()
    // Wait until no new re-fit lands across a flush window: async chrome above
    // the editor (onboarding banner mount, capacity poll) must have settled
    // before we baseline, or its legitimate mount could be mistaken for a
    // viewport-write re-fit below.
    await expect
      .poll(
        async () => {
          const before = await measures()
          await flushRefits()
          return (await measures()) === before
        },
        { timeout: 10_000, message: 'mobile fit should settle before baselining' },
      )
      .toBe(true)
    const baseline = await measures()

    // VueFlow's pan/zoom/node-drag hot path: the viewport transform is
    // rewritten as an inline style on every pointermove. Replicate it across
    // frames — none of these writes may schedule a re-measure (they used to
    // force a synchronous layout once per animation frame via the former
    // body-wide attribute observer).
    const pane = page.locator('.vue-flow__transformationpane').first()
    await expect(pane).toBeVisible()
    await page.evaluate(
      () =>
        new Promise<void>((resolve, reject) => {
          const paneEl = document.querySelector<HTMLElement>('.vue-flow__transformationpane')
          if (!paneEl) {
            reject(new Error('vue-flow transformationpane not found'))
            return
          }
          let step = 0
          const tick = () => {
            step += 1
            paneEl.style.transform = `translate(${step * 6}px, ${step * 4}px) scale(1.05)`
            if (step >= 6) {
              // Two extra frames so a (wrongly) scheduled re-fit would land.
              requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
            } else {
              requestAnimationFrame(tick)
            }
          }
          requestAnimationFrame(tick)
        }),
    )
    expect(await measures()).toBe(baseline)

    // The pin must be active on this viewport (375px < 768px).
    const heightBefore = parseInt((await root.evaluate((el) => (el as HTMLElement).style.height)) || '0', 10)
    expect(heightBefore).toBeGreaterThan(0)

    // A new chrome block appearing ABOVE the editor (parent childList) must
    // re-fit: the editor pins shorter to stay below it.
    await page.evaluate(() => {
      const editorRoot = document.querySelector('[data-testid="pipeline-editor-root"]')
      if (!editorRoot?.parentElement) throw new Error('editor root not mounted')
      const spacer = document.createElement('div')
      spacer.id = 'e2e-chrome-spacer'
      spacer.style.height = '48px'
      editorRoot.parentElement.insertBefore(spacer, editorRoot)
    })
    await expect
      .poll(async () => parseInt((await root.evaluate((el) => (el as HTMLElement).style.height)) || '0', 10))
      .toBeLessThan(heightBefore)
    expect(await measures()).toBeGreaterThan(baseline)

    // Later mutations INSIDE that new block must re-fit too (the observer
    // re-scans the chrome siblings when the parent's children change).
    const heightAfterInsert = parseInt((await root.evaluate((el) => (el as HTMLElement).style.height)) || '0', 10)
    await page.evaluate(() => {
      const spacer = document.getElementById('e2e-chrome-spacer')
      if (!spacer) throw new Error('chrome spacer missing')
      spacer.style.height = '96px'
    })
    await expect
      .poll(async () => parseInt((await root.evaluate((el) => (el as HTMLElement).style.height)) || '0', 10))
      .toBeLessThan(heightAfterInsert)
  })
})

// Desktop fold fit (FAR-1851). The editor root used `h-[calc(100vh-3.5rem)]`,
// which accounted only for the mobile fixed header — but the breadcrumb row (and
// any banners) render ABOVE the editor, so the editor overflowed the fold by the
// chrome height (~50px in production, more with banners). The fix pins the
// editor to the space actually left below that chrome, at every breakpoint.
test.describe('Pipeline Editor desktop', { tag: '@regression' }, () => {
  test.setTimeout(90_000)
  test.use({ viewport: { width: 1280, height: 720 } })

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('editor fits the fold — no vertical page scroll on desktop', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    await expect(page.locator('.vue-flow').first()).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    // Chrome above the editor resolves asynchronously (banners mount/measure),
    // and each change re-fits — poll until the document settles with no
    // vertical scroll.
    await expect
      .poll(
        async () => page.evaluate(() => document.documentElement.scrollHeight - document.documentElement.clientHeight),
        { timeout: 15_000, message: 'desktop editor must fit the fold with no vertical page scroll' },
      )
      .toBeLessThanOrEqual(0)

    const viewportHeight = await page.evaluate(() => document.documentElement.clientHeight)
    const rootBox = await page.getByTestId('pipeline-editor-root').boundingBox()
    expect(rootBox).not.toBeNull()
    // The editor's bottom edge is within the viewport.
    expect(rootBox!.y + rootBox!.height).toBeLessThanOrEqual(viewportHeight + 1)
  })
})
