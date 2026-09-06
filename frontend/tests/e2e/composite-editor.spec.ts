import { test, expect, loginAsAdmin } from './setup/fixtures'

// Structural e2e for /composites/:id/editor (FAR-638, batch A). The composite
// editor loads its template + editor state from two endpoints and renders a
// Vue Flow canvas with a docked toolbar. Mirrors json-viewer.spec.ts: mock
// the :id endpoints with a fake id and navigate directly — structural
// assertions only, no seeded data.
const COMPOSITE_ID = 'e2e-composite-1'

const TEMPLATE = {
  id: COMPOSITE_ID,
  name: 'E2E Composite',
  version: '1.0.0',
  description: 'Composite used by the e2e editor spec',
  parameter_ports_json: [],
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
}

const EDITOR_STATE = {
  nodes: [{ id: 'cnode-1', node_type: 'agent', label: 'Inner Agent', position: { x: 80, y: 80 } }],
  edges: [],
}

test.describe('Composite Editor', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server (plus Vue Flow) can exceed the 30s default before any
  // assertion runs. Mirrors json-viewer.spec.ts.
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // Force the Remy floating panel closed before the app boots so its
    // overlay can never cover page controls. Mirrors json-viewer.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('remy-panel-state', 'closed')
    })
  })

  test('renders toolbar and canvas for an existing composite', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/composite-templates/${COMPOSITE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(TEMPLATE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route(`**/api/v1/composite-templates/${COMPOSITE_ID}/editor`, (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EDITOR_STATE) })
    })

    await page.goto(`/composites/${COMPOSITE_ID}/editor`)

    await expect(page).toHaveURL(new RegExp(`/composites/${COMPOSITE_ID}/editor`))
    await expect(page.getByText('Back to Library')).toBeVisible()
    await expect(page.getByText('E2E Composite')).toBeVisible()
    // The Ports toggle is always rendered (canManage-gated Save-as/Publish are
    // role-dependent and deliberately NOT asserted here)
    await expect(page.getByRole('button', { name: 'Ports' })).toBeVisible()

    await expect(page.locator('.vue-flow').first()).toBeVisible()
    await expect(page.locator('.vue-flow__node').first()).toBeVisible()
  })
})
