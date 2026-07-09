import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Evals', () => {
  test('eval editor page loads', async ({ page, env }) => {
    await page.route('**/api/v1/evals*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'e1', name: 'Accuracy Eval', eval_type: 'exact_match', status: 'active', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/evals/editor')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Eval|Editor/i)
  })

  test('eval proposals page loads', async ({ page, env }) => {
    await page.route('**/api/v1/evals/proposals*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'ep1', name: 'Accuracy Improvement Proposal', eval_id: 'e1', status: 'pending', created_at: '2025-06-10T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/evals/proposals')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Proposal/i)
  })

  test('variants compare page loads', async ({ page, env }) => {
    await page.route('**/api/v1/variants*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'v1', name: 'GPT-4 vs Claude', pipeline_id: 'p1', status: 'ready', created_at: '2025-06-05T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/variants/compare')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Variant|Compare/i)
  })
})
