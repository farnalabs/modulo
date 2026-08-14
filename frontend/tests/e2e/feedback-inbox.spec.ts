import { test, expect, loginAsAdmin, isDevModeTarget, type Page } from './setup/fixtures'

const SAMPLE_RECORD = {
  id: 'rec-1',
  created_at: '2026-08-01T10:00:00Z',
  pipeline_name: 'Test Pipeline',
  rejection_reason: 'Output did not match schema',
  feedback_handler_type: 'human',
  feedback_status: 'pending',
}

const SAMPLE_DETAIL = {
  ...SAMPLE_RECORD,
  annotation: '',
  rejected_output: { key: 'value' },
  correction_proposal: null,
}

async function mockFeedbackInboxApi(page: Page, detailStatus: number) {
  await page.route(/\/api\/v1\/admin\/feature-flags$/, async (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ flags: [], license: { tier: 'enterprise' }, dev_mode: true }) })
  })
  await page.route(/\/api\/v1\/feedback\/inbox/, async (route) => {
    const url = route.request().url()
    const method = route.request().method()
    if (method === 'POST' && url.includes('/review')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SAMPLE_DETAIL) })
    }
    if (method === 'GET' && url.endsWith('/api/v1/feedback/inbox')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [SAMPLE_RECORD], total: 1 }) })
    }
    if (method === 'GET') {
      return route.fulfill({ status: detailStatus, contentType: 'application/json', body: detailStatus === 200 ? JSON.stringify(SAMPLE_DETAIL) : JSON.stringify({ detail: 'boom' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
}

test.describe('Feedback Inbox', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await mockFeedbackInboxApi(page, 200)
    await page.goto('/feedback/inbox')
    await expect(page.locator('h1')).toContainText('Feedback Inbox')
    await expect(page.getByTestId('feedback-inbox-title')).toBeVisible()
  })

  test('filter controls carry i18n aria-labels', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await mockFeedbackInboxApi(page, 200)
    await page.goto('/feedback/inbox')

    await expect(page.getByTestId('feedback-inbox-pipeline-select')).toHaveAttribute('aria-label', 'Pipeline')
    await expect(page.getByTestId('feedback-inbox-date-from')).toHaveAttribute('aria-label', 'From')
    await expect(page.getByTestId('feedback-inbox-date-to')).toHaveAttribute('aria-label', 'To')
  })

  test('expandable row exposes aria-expanded and the decorative chevron is aria-hidden', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await mockFeedbackInboxApi(page, 200)
    await page.goto('/feedback/inbox')

    const toggle = page.getByTestId('feedback-inbox-toggle-expand')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(toggle.locator('svg').first()).toHaveAttribute('aria-hidden', 'true')

    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByText('Rejection Reason')).toBeVisible()
  })

  test('detail error box uses role=alert', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await mockFeedbackInboxApi(page, 500)
    await page.goto('/feedback/inbox')

    await page.getByTestId('feedback-inbox-toggle-expand').click()

    const alertBox = page.locator('div[role="alert"]')
    await expect(alertBox).toBeVisible()
    await expect(alertBox.getByTestId('feedback-inbox-retry')).toBeVisible()
  })

  test('annotation result message uses role=status and aria-live=polite', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await mockFeedbackInboxApi(page, 200)
    await page.goto('/feedback/inbox')

    await page.getByTestId('feedback-inbox-toggle-expand').click()

    const annotation = page.getByTestId('feedback-inbox-annotation')
    await expect(annotation).toBeVisible()
    await annotation.fill('looks good')
    await page.getByTestId('feedback-inbox-save-annotation').click()

    const status = page.getByText('Annotation saved.')
    await expect(status).toHaveAttribute('role', 'status')
    await expect(status).toHaveAttribute('aria-live', 'polite')
  })
})
