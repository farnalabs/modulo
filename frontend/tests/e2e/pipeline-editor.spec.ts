import type { Page } from '@playwright/test'
import { test, expect, loginAsAdmin } from './setup/fixtures'

// Structural e2e for /pipelines/:id/editor (FAR-638, batch A). The editor
// route previously rotted broken with zero coverage (FAR-616/629), so these
// specs pin the FIXED behaviour: the page loads for an existing pipeline id,
// the toolbar + canvas render, and the save flow reaches the graph PUT.
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

  test('save flow reaches the graph PUT endpoint', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    const graphPuts: string[] = []
    await page.route(`**/api/v1/pipelines/${PIPELINE_ID}/graph`, (route) => {
      if (route.request().method() === 'PUT') {
        graphPuts.push(route.request().url())
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_GRAPH) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_GRAPH) })
    })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-save')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    await page.getByTestId('pipeline-editor-save').click()

    await expect.poll(() => graphPuts.length, {
      message: 'Save must issue a PUT to the pipeline graph endpoint',
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

  test('a short viewport yields the editor to the space left instead of a fixed floor', { tag: '@regression' }, async ({ page, env }) => {
    // 640x480 (short landscape) is where the chrome stack above the editor
    // exceeds the 240px usability floor the original mobile fix introduced:
    // with a hard floor the editor was forced to 240px and spilled ~80px below
    // the fold, re-introducing the trapped page scroll FAR-1841 removed. The
    // floor must yield to the viewport (FAR-1861) — the editor shrinks to the
    // space left instead of overflowing.
    //
    // The chrome height (fixed mobile header + breadcrumb + any ambient banners
    // such as the DB-capacity or onboarding banner) differs by environment, so
    // 640x480 does NOT guarantee a sub-240px leftover — on staging the chrome is
    // only ~110px, leaving ~370px, and a fixed 640x480 assertion failed. Measure
    // the chrome and shrink the height so ~180px remains below it: that
    // deterministically exercises the floor-yields case with or without a
    // banner. The invariant is CHROME-RELATIVE (the pinned height equals the
    // room left below the chrome), never a fixed pixel count: the mocked e2e
    // chrome is shorter than production's, so an absolute `< 240` here would be
    // asserting the mock's chrome height, not the floor yielding.
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)
    await page.setViewportSize({ width: 640, height: 480 })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    const flow = page.locator('.vue-flow').first()
    await expect(flow).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })

    // The fit resolves async (chrome above the editor mounts/measures on its
    // own schedule) — poll until the document settles with no vertical scroll.
    const settle = () =>
      expect
        .poll(
          async () => page.evaluate(() => document.documentElement.scrollHeight - document.documentElement.clientHeight),
          { timeout: 15_000, message: 'short viewport must not scroll the page' },
        )
        .toBeLessThanOrEqual(0)
    await settle()

    const root = page.getByTestId('pipeline-editor-root')
    let rootBox = await root.boundingBox()
    expect(rootBox).not.toBeNull()

    // Put the editor squarely in the "available < floor" band regardless of the
    // mocked-vs-production chrome height: shrink the viewport to the measured
    // chrome height plus a sub-240 slice. The resize handler re-fits the pin.
    const chromeHeight = Math.ceil(rootBox!.y)
    await page.setViewportSize({ width: 640, height: chromeHeight + 180 })
    await settle()

    const viewportHeight = await page.evaluate(() => document.documentElement.clientHeight)
    rootBox = await root.boundingBox()
    expect(rootBox).not.toBeNull()

    const available = viewportHeight - rootBox!.y
    // Precondition: genuinely below the old floor, so this only passes if the
    // floor yielded (a hard 240px floor would leave this at 240, not available).
    expect(available).toBeLessThan(240)
    expect(rootBox!.height).toBeGreaterThan(0)
    // The editor is the space actually left below the chrome — not a fixed pin.
    expect(Math.abs(rootBox!.height - available)).toBeLessThanOrEqual(1)
    // …and ends at or above the fold, so nothing is trapped below it.
    expect(rootBox!.y + rootBox!.height).toBeLessThanOrEqual(viewportHeight + 1)
  })

  test('a short landscape viewport scrolls the editor internally, never the page', { tag: '@regression' }, async ({ page, env }) => {
    // 640x360 (short landscape) is where the chrome stack above the editor
    // leaves less space than the wrapped toolbar's own min-content height
    // (measured ~124px at 640px wide, at root-top ≈322px → ~38px left). Pinning
    // the root there used to paint the toolbar below the fold with no page
    // scroll to reach it (FAR-1861). The fix clips the root and makes the
    // editor's content column the scroll container, so both the toolbar and the
    // canvas stay reachable without ever scrolling the page.
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)
    await page.setViewportSize({ width: 640, height: 360 })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    const flow = page.locator('.vue-flow').first()
    await expect(flow).toHaveCount(1, { timeout: EDITOR_READY_TIMEOUT })

    // The fit resolves async (chrome above mounts/measures on its own schedule).
    await expect
      .poll(
        async () => page.evaluate(() => document.documentElement.scrollHeight - document.documentElement.clientHeight),
        { timeout: 15_000, message: 'short landscape viewport must not scroll the page' },
      )
      .toBeLessThanOrEqual(0)

    // The root clips — nothing paints below the pinned box.
    const root = page.getByTestId('pipeline-editor-root')
    expect(await root.evaluate((el) => getComputedStyle(el).overflow)).toBe('hidden')

    // The editor's own content column is the scroll container, so the
    // sub-toolbar pinned height does not strand any control.
    const content = page.getByTestId('pipeline-editor-content')
    const { clientHeight, scrollHeight } = await content.evaluate((el) => ({
      clientHeight: el.clientHeight,
      scrollHeight: el.scrollHeight,
    }))
    expect(scrollHeight).toBeGreaterThan(clientHeight)

    const viewportHeight = await page.evaluate(() => document.documentElement.clientHeight)

    // Scroll the toolbar's bottom edge to the fold: every toolbar row is
    // reachable (it is not silently clipped away).
    const toolbarBox = await page.getByTestId('pipeline-editor-toolbar').boundingBox()
    expect(toolbarBox).not.toBeNull()
    await content.evaluate((el, dy) => { el.scrollTop = dy }, Math.max(0, toolbarBox!.height - clientHeight))
    const toolbarReached = await page.getByTestId('pipeline-editor-toolbar').boundingBox()
    expect(toolbarReached).not.toBeNull()
    expect(toolbarReached!.y + toolbarReached!.height).toBeLessThanOrEqual(viewportHeight + 1)

    // Scroll to the bottom: the canvas is reachable too, with real height.
    await content.evaluate((el) => { el.scrollTop = el.scrollHeight })
    await expect(flow).toBeVisible()
    const flowBox = await flow.boundingBox()
    expect(flowBox).not.toBeNull()
    expect(flowBox!.height).toBeGreaterThan(0)
    expect(flowBox!.y + flowBox!.height).toBeLessThanOrEqual(viewportHeight + 1)
  })

  // FAR-1899: the toolbar popovers must survive the sub-fit band. FAR-1861
  // clipped the editor root and made the content column the internal scroll
  // container; a popover left in-tree is clipped to the column's box, which in
  // the sub-fit band is shorter than the wrapped toolbar. The mocked e2e chrome
  // is shorter than production's, so the viewport alone does not enter the band
  // — chrome is added above the editor to make the column shorter than the
  // toolbar's min-content height, exactly the condition the ticket describes.
  async function enterSubFitBand(page: Page) {
    await page.evaluate(() => {
      const editorRoot = document.querySelector('[data-testid="pipeline-editor-root"]')
      if (!editorRoot?.parentElement) throw new Error('editor root not mounted')
      const spacer = document.createElement('div')
      spacer.id = 'e2e-subfit-chrome-spacer'
      spacer.style.height = '160px'
      editorRoot.parentElement.insertBefore(spacer, editorRoot)
    })
    const content = page.getByTestId('pipeline-editor-content')
    const toolbar = page.getByTestId('pipeline-editor-toolbar')
    await expect
      .poll(async () => content.evaluate((el) => el.clientHeight), {
        timeout: 10_000,
        message: 'editor content column must shrink below the toolbar min-content height',
      })
      .toBeLessThan(await toolbar.evaluate((el) => el.getBoundingClientRect().height))
  }

  test('the sub-fit band keeps the toolbar dropdown unclipped at the fold', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)
    await page.setViewportSize({ width: 640, height: 360 })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    await enterSubFitBand(page)

    await page.getByTestId('pipeline-editor-node-type-select').click()
    const overlay = page.locator('.p-select-overlay')
    await expect(overlay).toBeVisible()

    const viewport = page.viewportSize()!
    const box = await overlay.boundingBox()
    expect(box).not.toBeNull()
    // Fully inside the fold on every edge — not clipped where the content
    // column would have cut it, and not detached off-screen.
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height)
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width)

    // …and actually anchored to its TRIGGER, not merely "somewhere on screen".
    // An overlay teleported but detached to the viewport origin (0,0) — the
    // exact FAR-851 regression body-append exists to prevent — satisfies every
    // viewport-only assertion above, so pin the overlay's horizontal range to
    // the trigger's and its near edge to the trigger's bottom (or top when
    // PrimeVue flips it above).
    const triggerBox = await page.getByTestId('pipeline-editor-node-type-select').boundingBox()
    expect(triggerBox).not.toBeNull()
    const overlapsTriggerX =
      box!.x < triggerBox!.x + triggerBox!.width && triggerBox!.x < box!.x + box!.width
    expect(overlapsTriggerX).toBe(true)

    // PrimeVue places the overlay flush against the trigger's near edge (its
    // own anchor gutter is at most a couple of px); allow a small slack.
    const nearEdgeSlack = 8
    const flippedAbove = box!.y + box!.height <= triggerBox!.y
    if (flippedAbove) {
      expect(Math.abs(triggerBox!.y - (box!.y + box!.height))).toBeLessThanOrEqual(nearEdgeSlack)
    } else {
      expect(Math.abs(box!.y - (triggerBox!.y + triggerBox!.height))).toBeLessThanOrEqual(nearEdgeSlack)
    }

    // A clipped box still reports a rect, so also prove the overlay is
    // hit-testable at its own centre (i.e. actually visible, not cut off).
    const hitAtCentre = await page.evaluate(() => {
      const el = document.querySelector('.p-select-overlay') as HTMLElement | null
      if (!el) return false
      const r = el.getBoundingClientRect()
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      return top !== null && el.contains(top)
    })
    expect(hitAtCentre).toBe(true)

    // Teleporting the overlay must not break its keyboard dismissal: Escape on
    // the focused select still closes it.
    await page.keyboard.press('Escape')
    await expect(overlay).toBeHidden()
  })

  test('the sub-fit band keeps the retry-policy panel inside the fold', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockEditorWithNode(page)
    await page.setViewportSize({ width: 640, height: 360 })

    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
    await enterSubFitBand(page)

    await page.getByTestId('pipeline-editor-retry-policy-toggle').click()
    const panel = page.getByTestId('pipeline-editor-retry-policy-panel')
    await expect(panel).toBeVisible()

    const viewport = page.viewportSize()!
    const box = await panel.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height)

    // The panel is bounded to the room available and scrolls its own content,
    // instead of being cut off at the fold. Strict `>` — `>=` is always true
    // (`scrollHeight` can never be below `clientHeight`) and asserts nothing.
    const scrollsInternally = await panel.evaluate(
      (el) => getComputedStyle(el).overflowY === 'auto' && el.scrollHeight > el.clientHeight,
    )
    expect(scrollsInternally).toBe(true)
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
    // Count measurements of the editor root: refitEditorFit's
    // getBoundingClientRect is the only page code that reads that element,
    // so each call == one scheduled re-fit of the editor height pin.
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
