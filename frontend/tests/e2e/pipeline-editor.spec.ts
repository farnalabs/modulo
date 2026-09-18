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

test.describe('Pipeline Editor', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server (plus Vue Flow) can exceed the 30s default before any
  // assertion runs. Mirrors json-viewer.spec.ts.
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // The Remy floating panel (rendered where dev-mode is on) opens by default
    // and its fixed-position overlay intercepts clicks on interactive page
    // content. Force the panel closed before the app boots. Mirrors
    // json-viewer.spec.ts / view-modes-admin.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('remy-panel-state', 'closed')
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
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible()
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
    await expect(page.getByTestId('pipeline-editor-save')).toBeVisible()

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

    await expect(page.locator('.vue-flow').first()).toBeVisible()
    await expect(page.locator('.vue-flow__node').first()).toBeVisible()
    // With a node present the Run button is enabled (disabled only when the graph is empty)
    await expect(page.getByTestId('pipeline-editor-run')).toBeEnabled()
  })
})
