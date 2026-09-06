import { test, expect, loginAsAdmin } from './setup/fixtures'
import type { Page } from '@playwright/test'

// Structural e2e for the lifecycle-maps routes (FAR-638, batch A):
//   /lifecycle-maps            — list page (+ empty state)
//   /lifecycle-maps/new        — redirects to the list (creation is a dialog)
//   /lifecycle-maps/:id        — detail page
//   /lifecycle-maps/:id/editor — map editor
// and the create flow (New Map dialog → POST → editor). Mirrors
// json-viewer.spec.ts: mock the API with fake ids and navigate directly —
// structural assertions only, no seeded data.
const MAP_ID = 'e2e-map-1'

const MAP_SUMMARY = {
  id: MAP_ID,
  name: 'Delivery Lifecycle',
  description: 'Map used by the e2e lifecycle spec',
  owner: 'admin@example.com',
  owner_team_id: null,
  stage_count: 1,
  graduated_count: 0,
  current_version: 1,
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
}

// Shape consumed by the store detail view (LifecycleMapView).
const MAP_DETAIL = {
  id: MAP_ID,
  name: 'Delivery Lifecycle',
  description: 'Map used by the e2e lifecycle spec',
  owner: 'admin@example.com',
  owner_team_id: null,
  stages: [],
  transitions: [],
  versions: [{ version: 1, created_at: '2026-01-01T10:00:00Z', created_by: null }],
  current_version: 1,
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
}

// Shape consumed by the map editor (LifecycleMapEditor builds flow nodes from
// version.stages with stage_type and version.edges with *_stage_id).
const MAP_VERSIONS = [
  {
    id: 'ver-1',
    lifecycle_map_id: MAP_ID,
    version_number: 1,
    stages: [
      {
        id: 'stage-1',
        name: 'Implement',
        description: 'Build the thing',
        stage_type: 'manual',
        pipeline_id: null,
        external_url: null,
        owner: null,
        graduated: false,
        x: 100,
        y: 100,
      },
    ],
    edges: [],
    created_by: 'e2e',
    created_at: '2026-01-01T10:00:00Z',
    notes: 'seeded by e2e',
  },
]

function mockMapDetailRoutes(page: Page) {
  page.route(`**/api/v1/lifecycle-maps/${MAP_ID}`, (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MAP_DETAIL) })
  })
  page.route(`**/api/v1/lifecycle-maps/${MAP_ID}/versions`, (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MAP_VERSIONS) })
  })
  page.route(`**/api/v1/lifecycle-maps/${MAP_ID}/journeys*`, (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], next_cursor: null }) })
  })
}

test.describe('Lifecycle Maps', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server can exceed the 30s default before any assertion runs.
  // Mirrors json-viewer.spec.ts.
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // Force the Remy floating panel closed before the app boots so its
    // overlay can never cover page controls. Mirrors json-viewer.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('remy-panel-state', 'closed')
    })
  })

  test('renders the list page with an empty state', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/lifecycle-maps', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/lifecycle-maps')

    await expect(page).toHaveURL(/\/lifecycle-maps/)
    await expect(page.locator('h1')).toContainText('Lifecycle Maps')
    await expect(page.getByTestId('lifecycle-map-list-new')).toBeVisible()
    await expect(page.getByTestId('lifecycle-map-list-empty-new')).toBeVisible()
  })

  test('renders map cards from the summaries', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/lifecycle-maps', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [MAP_SUMMARY] }) })
    })

    await page.goto('/lifecycle-maps')

    const card = page.getByTestId('lifecycle-map-list-card')
    await expect(card).toHaveCount(1)
    await expect(card).toContainText('Delivery Lifecycle')
    await expect(page.getByTestId('lifecycle-map-list-owner-filter')).toBeVisible()
    await expect(page.getByTestId('lifecycle-map-list-new')).toBeVisible()
  })

  test('redirects /lifecycle-maps/new to the list page', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/lifecycle-maps', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/lifecycle-maps/new')

    await expect(page).toHaveURL(/\/lifecycle-maps$/)
    await expect(page.locator('h1')).toContainText('Lifecycle Maps')
  })

  test('creates a map through the New Map dialog and opens the editor', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/lifecycle-maps', (route) => {
      if (route.request().method() === 'POST') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MAP_SUMMARY) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    mockMapDetailRoutes(page)

    await page.goto('/lifecycle-maps')
    await page.getByTestId('lifecycle-map-list-new').click()

    const dialog = page.locator('div.fixed.inset-0', { hasText: 'Create Lifecycle Map' })
    await expect(dialog.getByText('Create Lifecycle Map')).toBeVisible()

    await dialog.locator('#lifecyclemaplist-field-2').fill('Delivery Lifecycle')
    await dialog.getByRole('button', { name: 'Create', exact: true }).click()

    // Creating navigates straight into the editor for the new map
    await expect(page).toHaveURL(new RegExp(`/lifecycle-maps/${MAP_ID}/editor`))
    await expect(page.locator('.vue-flow').first()).toBeVisible()
  })

  test('renders the map detail page', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    mockMapDetailRoutes(page)

    await page.goto(`/lifecycle-maps/${MAP_ID}`)

    await expect(page).toHaveURL(new RegExp(`/lifecycle-maps/${MAP_ID}`))
    await expect(page.locator('h1')).toContainText('Delivery Lifecycle')
    await expect(page.getByTestId('lifecycle-map-view-edit')).toBeVisible()
    await expect(page.getByTestId('lifecycle-map-view-delete')).toBeVisible()
    await expect(page.getByTestId('lifecycle-map-export')).toBeVisible()
    await expect(page.getByTestId('lifecycle-map-import')).toBeVisible()
  })

  test('renders the map editor with stages', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    mockMapDetailRoutes(page)

    await page.goto(`/lifecycle-maps/${MAP_ID}/editor`)

    await expect(page).toHaveURL(new RegExp(`/lifecycle-maps/${MAP_ID}/editor`))
    await expect(page.getByText('Back to Lifecycle Maps')).toBeVisible()
    await expect(page.getByText('Delivery Lifecycle')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Auto Layout' })).toBeVisible()
    await expect(page.locator('div.absolute.top-3').getByRole('button', { name: 'Save' })).toBeVisible()

    // The version's stages render on the Vue Flow canvas
    await expect(page.locator('.vue-flow').first()).toBeVisible()
    await expect(page.locator('.vue-flow__node').first()).toBeVisible()
  })
})
