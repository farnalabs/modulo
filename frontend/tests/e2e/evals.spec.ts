import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Evals', { tag: "@regression" }, () => {
  test('eval editor page loads', { tag: "@regression" }, async ({ page, env }) => {
    await page.route('**/api/v1/evals*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'e1', name: 'Accuracy Eval', eval_type: 'exact_match', status: 'active', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/evals/editor')

    await expect(page.locator('h1')).toContainText(/Eval|Editor/i)
  })

  test('eval proposals page loads', { tag: "@regression" }, async ({ page, env }) => {
    await page.route('**/api/v1/feedback/proposals*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'ep1', run_id: 'r1', gate_id: 'g1', rejected_by: null, rejection_reason: 'Accuracy below 90% threshold', rejected_output: {}, producing_node_id: 'pn1', producing_node_name: 'Evaluation Node', producing_agent_id: null, feedback_status: 'pending', feedback_handler_type: 'eval_gap', correction_run_id: null, eval_gap: true, needs_human_review: false, pipeline_name: 'Test Pipeline', created_at: '2025-06-10T10:00:00Z' }], total: 1, page: 1, page_size: 20 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/evals/proposals')

    await expect(page.locator('h1')).toContainText(/Proposal/i)
  })

  test('variants compare page loads', { tag: "@regression" }, async ({ page, env }) => {
    await page.route('**/api/v1/variants*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'v1', name: 'GPT-4 vs Claude', pipeline_id: 'p1', status: 'ready', created_at: '2025-06-05T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/variants/compare')

    await expect(page.locator('h1')).toContainText(/Variant|Compare/i)
  })
})
