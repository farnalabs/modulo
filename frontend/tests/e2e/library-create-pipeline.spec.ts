import { test, expect, loginAsAdmin } from './setup/fixtures'

// Structural e2e for /library/:id/create-pipeline (FAR-638, batch A). The
// wizard loads a library primitive, pre-fills the pipeline name/description
// from it, and the Create button reaches the create-pipeline endpoint. Mirrors
// json-viewer.spec.ts: mock the :id endpoint with a fake id and navigate
// directly — structural assertions only, no seeded data.
const PRIMITIVE_ID = 'e2e-primitive-1'

const PRIMITIVE = {
  id: PRIMITIVE_ID,
  primitive_type: 'pipeline_template',
  name: 'E2E Code Review Template',
  description: 'Reviews pull requests',
  author: 'Modulo Team',
  version: '1.0.0',
  tags: ['review'],
  visibility: 'public',
  content_json: {
    agents: [
      {
        name: 'Reviewer',
        description: 'Reviews code',
        connector_type_refs: [{ connector_type: 'github' }],
      },
    ],
  },
}

test.describe('Library Create Pipeline Wizard', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page }) => {
    // Force the Remy floating panel closed before the app boots so its
    // overlay can never cover page controls. Mirrors json-viewer.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('remy-panel-state', 'closed')
    })
  })

  test('renders the wizard pre-filled from the primitive', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/libraries/${PRIMITIVE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PRIMITIVE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })

    await page.goto(`/library/${PRIMITIVE_ID}/create-pipeline`)

    await expect(page).toHaveURL(new RegExp(`/library/${PRIMITIVE_ID}/create-pipeline`))
    await expect(page.locator('h1')).toContainText('Create Pipeline from Template')
    await expect(page.getByTestId('library-wizard-back')).toBeVisible()
    await expect(page.getByTestId('library-wizard-pipeline-name')).toHaveValue('E2E Code Review Template')
    await expect(page.getByTestId('library-wizard-description')).toHaveValue('Reviews pull requests')
    await expect(page.getByTestId('library-wizard-create')).toBeVisible()
    await expect(page.getByTestId('library-wizard-cancel')).toBeVisible()
  })

  test('creates a pipeline and links to it', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route(`**/api/v1/libraries/${PRIMITIVE_ID}`, (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PRIMITIVE) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route(`**/api/v1/libraries/${PRIMITIVE_ID}/create-pipeline`, (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'e2e-new-pipe',
          name: 'E2E Code Review Template',
          description: 'Reviews pull requests',
          template_source_id: PRIMITIVE_ID,
          agent_count: 1,
          edge_count: 0,
          ready_to_run: true,
          created_at: '2026-01-01T10:00:00Z',
          updated_at: '2026-01-01T10:00:00Z',
        }),
      })
    })

    await page.goto(`/library/${PRIMITIVE_ID}/create-pipeline`)
    await expect(page.getByTestId('library-wizard-pipeline-name')).toHaveValue('E2E Code Review Template')

    await page.getByTestId('library-wizard-create').click()

    await expect(page.getByText('Pipeline created!')).toBeVisible()
    await expect(page.getByTestId('library-wizard-view-pipeline')).toHaveAttribute('href', /pipelines\/e2e-new-pipe/)
  })
})
