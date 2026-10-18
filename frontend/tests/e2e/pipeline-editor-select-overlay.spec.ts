import { test, expect, loginAsAdmin } from './setup/fixtures'
import type { Locator, Page } from '@playwright/test'

/**
 * FAR-1946 — the pipeline editor's Selects each carry an explicit
 * `append-to="body"` (added by FAR-1899, #1937). This spec pins that the
 * explicit target does what the FAR-1920 predicate otherwise derives: the
 * overlay is portalled OUT of the editor's clipping ancestors, so the drawn
 * options are the topmost element at their own centre.
 *
 * The editor's Selects sit inside clipping ancestors that are NOT dialogs —
 * the editor content column (`overflow-y-auto`) and the edge-properties panel
 * (`overflow-y-auto`) — which is exactly the FAR-1920 defect class. With
 * `'self'` the overlay is confined to that box and its lower options are
 * covered by whatever paints over them; with `'body'` it escapes.
 *
 * Five sites are covered: the toolbar node-type Select, the empty-state
 * node-type Select, and the edge-properties edge-type / condition-type /
 * eval-operator Selects. The assertion is the invariant (overlay outside every
 * clipping ancestor + option hit-testable at its centre), not a PrimeVue click
 * that would auto-scroll and mask a covered option.
 */

const PIPELINE_ID = 'e2e-editor-select-overlay'

const PIPELINE = {
  id: PIPELINE_ID,
  name: 'E2E Editor Select Overlay',
  organisation_id: '1',
  description: 'pipeline used by the FAR-1946 select-overlay spec',
  visibility: 'org',
  status: 'idle',
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
  archived_at: null,
}

const NODES = [
  { id: 'node-1', node_type: 'agent', label: 'Node A', position: { x: 120, y: 180 } },
  { id: 'node-2', node_type: 'agent', label: 'Node B', position: { x: 460, y: 180 } },
]
const EDGE = {
  id: 'edge-1',
  source_node_id: 'node-1',
  target_node_id: 'node-2',
  edge_type: 'normal',
}

async function mockEditor(page: Page, nodes: unknown[], edges: unknown[]) {
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
      body: JSON.stringify({ nodes, edges }),
    })
  })
}

/**
 * Proven for one open Select: the trigger sits inside a clipping ancestor
 * (non-vacuity), the overlay itself is NOT inside any clipping ancestor, and
 * every option that is inside the overlay's visible box is the topmost element
 * at its own centre.
 */
async function expectOverlayEscapesClipping(page: Page, trigger: Locator, label: string) {
  // Non-vacuity: the trigger really is hosted inside a clipping ancestor, so
  // "the overlay escaped" is meaningful rather than trivially true.
  const hostedInClip = await trigger.evaluate((el) => {
    const CLIPPING = new Set(['hidden', 'clip', 'auto', 'scroll'])
    let node: Element | null = el
    while (node && node !== document.body && node !== document.documentElement) {
      const s = getComputedStyle(node)
      if (CLIPPING.has(s.overflowX) || CLIPPING.has(s.overflowY)) return true
      node = node.parentElement
    }
    return false
  })
  expect(hostedInClip, `${label}: trigger must sit inside a clipping ancestor`).toBe(true)

  await trigger.click()
  await expect(page.locator('.p-select-overlay')).toBeVisible()

  const escaped = await page.evaluate(() => {
    const overlay = document.querySelector('.p-select-overlay')
    if (!overlay) return { found: false, container: 'MISSING' }
    const CLIPPING = new Set(['hidden', 'clip', 'auto', 'scroll'])
    let node: Element | null = overlay.parentElement
    while (node && node !== document.body && node !== document.documentElement) {
      const s = getComputedStyle(node)
      if (CLIPPING.has(s.overflowX) || CLIPPING.has(s.overflowY)) {
        const cls = String(node.className).split(/\s+/).slice(0, 2).join('.')
        return { found: true, container: `${node.tagName.toLowerCase()}.${cls}` }
      }
      node = node.parentElement
    }
    return { found: true, container: '' }
  })
  expect(escaped.found, `${label}: overlay element must exist`).toBe(true)
  expect(escaped.container, `${label}: overlay still inside clipping ancestor ${escaped.container}`).toBe('')

  const probe = await page.evaluate(() => {
    const overlay = document.querySelector('.p-select-overlay')
    if (!overlay) return { count: 0, misses: [{ text: 'no-overlay', hit: '' }] }
    const or = overlay.getBoundingClientRect()
    const inViewportAndOverlay = Array.from(
      overlay.querySelectorAll('.p-select-overlay .p-select-option, .p-select-option'),
    ).filter((el) => {
      const r = el.getBoundingClientRect()
      if (r.height <= 0) return false
      const withinOverlay = r.top >= or.top - 1 && r.bottom <= or.bottom + 1
      const withinViewport = r.top >= -1 && r.bottom <= window.innerHeight + 1
      return withinOverlay && withinViewport
    })
    const misses = inViewportAndOverlay.flatMap((el) => {
      const r = el.getBoundingClientRect()
      const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
      if (el === top || (top != null && el.contains(top))) return []
      return [{ text: (el.textContent ?? '').trim(), hit: (top as HTMLElement | null)?.className ?? 'null' }]
    })
    return { count: inViewportAndOverlay.length, misses }
  })
  expect(probe.count, `${label}: at least one option must be drawn inside the overlay`).toBeGreaterThan(0)
  expect(probe.misses, `${label}: options covered at their centre`).toHaveLength(0)

  await page.keyboard.press('Escape')
  await expect(page.locator('.p-select-overlay')).toBeHidden()
}

test.describe('Pipeline editor Select overlays escape their clipping ancestors (FAR-1946)', { tag: '@regression' }, () => {
  test.setTimeout(90_000)

  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name !== 'local', 'local mock only')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
    await loginAsAdmin(page, env)
  })

  const EDITOR_READY_TIMEOUT = 30_000

  async function openEditor(page: Page) {
    await page.goto(`/pipelines/${PIPELINE_ID}/editor`)
    await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: EDITOR_READY_TIMEOUT })
  }

  test('toolbar node-type Select (site 1) portals its overlay out of the clipped editor column', async ({ page }) => {
    await mockEditor(page, NODES, [])
    await openEditor(page)

    await expectOverlayEscapesClipping(
      page,
      page.getByTestId('pipeline-editor-node-type-select'),
      'toolbar node-type Select',
    )
  })

  test('empty-state node-type Select (site 2) portals its overlay out of the clipped column', async ({ page }) => {
    await mockEditor(page, [], [])
    await openEditor(page)

    await expect(page.getByTestId('pipeline-editor-empty-node-type-select')).toBeVisible()
    await expectOverlayEscapesClipping(
      page,
      page.getByTestId('pipeline-editor-empty-node-type-select'),
      'empty-state node-type Select',
    )
  })

  test('edge-properties Selects (sites 3-5) portal their overlays out of the clipped panel', async ({ page }) => {
    await mockEditor(page, NODES, [EDGE])
    await openEditor(page)

    // Open the edge inspector by clicking the rendered edge (its wide
    // interaction path is the pointer target).
    await page.locator('.vue-flow__edge-interaction').first().click()
    await expect(page.getByText('Edge Properties')).toBeVisible()

    const panel = page.locator('aside').filter({ hasText: 'Edge Properties' })

    // Site 3 — edge type.
    await expectOverlayEscapesClipping(
      page,
      panel.getByRole('combobox', { name: 'Edge type' }),
      'edge-type Select',
    )

    // The condition-type / eval-operator Selects only render once the edge's
    // HITL gate is enabled, so turn it on before exercising them.
    await panel.getByRole('checkbox', { name: 'Enabled' }).check()

    // Site 4 — condition type.
    const conditionSelect = panel.getByRole('combobox', { name: 'Condition Type' })
    await expectOverlayEscapesClipping(page, conditionSelect, 'condition-type Select')

    // Site 5 — eval operator, which only renders when condition type is `eval`.
    await conditionSelect.click()
    await page.locator('.p-select-overlay .p-select-option').filter({ hasText: 'Eval Reference' }).click()
    await expect(page.locator('.p-select-overlay')).toBeHidden()

    await expectOverlayEscapesClipping(
      page,
      panel.getByRole('combobox', { name: 'Operator' }),
      'eval-operator Select',
    )
  })
})
