import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * FAR-2396: targeted phone-width regression checks for the four surfaces the
 * ticket reports. This is deliberately NOT the cross-route overflow sweep
 * (FAR-2397) — it mocks just enough data to render each affected surface at
 * 320px and asserts the changed elements fit their own container, that text
 * wraps rather than clipping, and that the Costs font override is scoped.
 *
 * Local-only: it installs route mocks on top of the shared local fixture.
 */
test.use({ viewport: { width: 320, height: 568 } })

const GATE = {
  run_id: '550e8400-e29b-41d4-a716-446655440000',
  review_id: 'approval-gate-1',
  pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
  pipeline_name: 'Release pipeline with a deliberately long descriptive name',
  label: 'Deployment approval gate requiring a named reviewer',
  description: 'Checks the release artefacts before promoting the deployment to production.',
  claimed_by: null,
  claimed_at: null,
  expires_at: null,
  decision: null,
  decision_at: null,
  created_at: '2025-06-30T10:00:00Z',
}

const TEAM = {
  id: 't1',
  name: 'Very long engineering team name that must stay readable',
  description: 'Platform and infrastructure engineering',
  account_id: 'a1',
  member_count: 0,
  owned_resource_count: 0,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
}

const COST_COMPONENTS = [
  {
    id: 'comp-1',
    name: 'llm_tokens',
    display_name: 'LLM Tokens',
    kind: 'calculated',
    rate_usd: null,
    rate_fallback: null,
    formula: 'tokens_input * input_token_rate + tokens_output * output_token_rate',
    report_key: null,
    enabled: true,
    sort_order: 0,
    deleted_at: null,
  },
  {
    id: 'comp-2',
    name: 'model_tokens',
    display_name: 'Model Tokens',
    kind: 'self_reported',
    rate_usd: null,
    rate_fallback: null,
    formula: null,
    report_key: 'model_cost_usd',
    enabled: true,
    sort_order: 1,
    deleted_at: null,
  },
]

async function expectNoPageOverflow(page: import('@playwright/test').Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow).toBeLessThanOrEqual(1)
}

async function expectInside(
  child: import('@playwright/test').Locator,
  parent: import('@playwright/test').Locator,
) {
  const [childBox, parentBox] = await Promise.all([child.boundingBox(), parent.boundingBox()])
  expect(childBox).not.toBeNull()
  expect(parentBox).not.toBeNull()
  expect(childBox!.x).toBeGreaterThanOrEqual(parentBox!.x - 0.5)
  expect(childBox!.x + childBox!.width).toBeLessThanOrEqual(parentBox!.x + parentBox!.width + 0.5)
}

test.describe('FAR-2396 mobile responsiveness', { tag: '@regression' }, () => {
  test('Agent Output Diff controls fit their card at 320px', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'route mocks are local-only')
    await loginAsAdmin(page, env)
    await page.goto('/runs/diff')

    const card = page.getByTestId('diff-controls-card')
    await expect(card).toBeVisible()

    for (const testId of [
      'diff-recent-runs-a',
      'diff-run-id-a',
      'diff-node-id',
      'diff-recent-runs-b',
      'diff-run-id-b',
    ]) {
      const control = page.getByTestId(testId)
      await expect(control).toBeVisible()
      await expectInside(control, card)
    }
    await expectNoPageOverflow(page)
  })

  test('HITL Review row stacks and keeps select-all reachable at 320px', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'route mocks are local-only')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/hitl/reviews**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [GATE], total: 1, page: 1, page_size: 25 }),
      }),
    )
    await page.goto('/settings/hitl-review')

    await expect(page.getByTestId('hitl-review-select-all')).toBeVisible()
    const row = page.getByTestId('hitl-review-toggle-expand')
    await expect(row).toBeVisible()
    await expectInside(row, page.locator('main'))

    const pipeline = page.getByTestId('hitl-review-pipeline-name')
    await expect(pipeline).toBeVisible()
    const clipped = await pipeline.evaluate((el) => el.scrollWidth > el.clientWidth + 1)
    expect(clipped).toBe(false)
    await expectNoPageOverflow(page)
  })

  test('Teams name stays readable and metadata stacks below it at 320px', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'route mocks are local-only')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/admin/teams**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [TEAM], total: 1 }),
      }),
    )
    await page.goto('/settings/teams')

    const teamRow = page.getByTestId('settings-teams-row').first()
    await expect(teamRow).toBeVisible()
    const name = page.getByTestId('settings-teams-team-name').first()
    await expect(name).toBeVisible()
    await expectInside(name, teamRow)

    const nameBox = await name.boundingBox()
    const metaBox = await page.getByTestId('settings-teams-owned-resource-count').first().boundingBox()
    expect(nameBox).not.toBeNull()
    expect(metaBox).not.toBeNull()
    expect(nameBox!.width).toBeGreaterThan(80)
    expect(metaBox!.y).toBeGreaterThanOrEqual(nameBox!.y + nameBox!.height - 1)
    await expectNoPageOverflow(page)
  })

  test('Costs ordinary labels use Inter while technical values stay monospace', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'route mocks are local-only')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/admin/costs/components**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(COST_COMPONENTS),
      }),
    )
    await page.goto('/admin/costs/components')

    const heading = page.locator('h1')
    await expect(heading).toBeVisible()
    const headingFont = await heading.evaluate((el) => getComputedStyle(el).fontFamily.toLowerCase())
    expect(headingFont).toContain('inter')

    const formula = page.locator('code.font-mono').first()
    await expect(formula).toBeVisible()
    const formulaFont = await formula.evaluate((el) => getComputedStyle(el).fontFamily.toLowerCase())
    expect(formulaFont).toContain('jetbrains mono')
    await expectNoPageOverflow(page)
  })
})
