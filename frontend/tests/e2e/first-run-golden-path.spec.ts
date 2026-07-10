import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('First-Run Golden Path', () => {

  test('golden path: login -> browse pipelines -> trigger run -> inspect output', async ({ page, env }) => {
    // ── Step 1: Navigate to app and log in ──────────────────────────
    await loginAsAdmin(page, env)

    // ── Mock API responses for the full golden path ──────────────────
    const RUN_ID = 'run-demo-001'

    // Pipeline trigger: PipelineListView posts to /api/v1/runs
    await page.route('**/api/v1/runs', async (route) => {
      if (route.request().method() === 'POST') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: RUN_ID }) })
      }
      return route.fallback()
    })

    // Pipeline list: return at least one pipeline so the run button renders
    await page.route('**/api/v1/pipelines*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: '1', name: 'Demo Pipeline', organisation_id: '1', description: 'A demo pipeline to test the golden path', visibility: 'org', status: 'idle', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', archived_at: null }], total: 1 }) })
    })

    // Run status poll: start queued, then running, then complete
    let pollCount = 0
    await page.route('**/api/v1/runs/*', async (route) => {
      pollCount++
      const statuses = ['queued', 'running', 'running', 'complete']
      const idx = Math.min(pollCount, statuses.length - 1)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        id: RUN_ID, pipeline_id: '1', status: statuses[idx],
        created_at: new Date(Date.now() - 60000).toISOString(),
        completed_at: statuses[idx] === 'complete' ? new Date().toISOString() : null,
        nodes: [
          { id: 'node-1', name: 'Input', type: 'input', status: 'complete', output: { text: 'Hello world' } },
          { id: 'node-2', name: 'Process', type: 'llm', status: 'complete', output: { text: 'Processed: Hello world' } },
          { id: 'node-3', name: 'Output', type: 'output', status: 'complete', output: { text: 'Final result: Processed: Hello world' } },
        ],
      }) })
    })

    // ── Step 2: Navigate to pipelines list ──────────────────────────
    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-search')).toBeVisible()

    // ── Step 3: Find the demo pipeline and open run dialog ──────────
    const runButton = page.getByTestId('pipeline-list-run').first()
    await expect(runButton).toBeVisible({ timeout: 5000 })
    await runButton.click()

    // ── Step 4: Trigger a run via the dialog ────────────────────────
    const submitButton = page.getByTestId('pipeline-list-run-submit')
    await expect(submitButton).toBeVisible()
    await submitButton.click()

    // ── Step 5: Wait for navigation to run detail and inspect output ─
    await page.waitForURL(`/runs/${RUN_ID}`, { timeout: 10000 })
    await page.waitForLoadState('networkidle')

    // Verify the run detail page loaded
    await expect(page.locator('h1')).toContainText(/Run|run/, { timeout: 5000 })

    // ── Step 6: Verify run output is visible ────────────────────────
    const completedBadge = page.locator('text=complete').first()
    await expect(completedBadge).toBeVisible({ timeout: 5000 })

    const nodeOutput = page.locator('text=Hello world').first()
    await expect(nodeOutput).toBeVisible({ timeout: 5000 })

    console.info('[golden-path] Golden path completed successfully')
  })

  test('dashboard shows pipeline summary for first-run user', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Dashboard should be visible with pipeline count or recent activity
    await expect(page.getByTestId('dashboard-title')).toBeVisible({ timeout: 5000 })
  })

  test('demo pipeline exists in library', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')

    // At least one pipeline should be visible (from the mock)
    const pipelineRows = page.locator('a[href*="/pipelines/"]')
    const count = await pipelineRows.count()
    expect(count).toBeGreaterThanOrEqual(1)
  })
})
