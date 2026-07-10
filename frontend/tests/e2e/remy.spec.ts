import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Remy Admin Configuration', () => {
  test('page loads with remy configuration sections', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // Remy config API
    await page.route('**/api/v1/admin/remy/config', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ access_list: { user_ids: [], team_ids: [], org_roles: [] }, default_provider: 'anthropic', default_model: 'claude-sonnet-4-20250514', default_context_window: 200000, allowed_providers: ['anthropic', 'openai'], allowed_models: [], system_prompt: '', additional_guidance: '', permission_mode: 'safe', tool_permissions: {}, rate_limit_max_actions: 30, rate_limit_window_seconds: 60, auto_execute_threshold: 0.8, nogo_page_patterns: [], nogo_selector_patterns: [], allowed_selectors: [], allowed_page_patterns: [] }) })
    })
    // Available providers (must return { native: [...], customTypes: [...] })
    await page.route('**/api/v1/admin/remy/available-providers', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ native: [{ id: 'anthropic', label: 'Anthropic' }, { id: 'openai', label: 'OpenAI' }], customTypes: [] }) })
    })
    // Skills
    await page.route('**/api/v1/admin/remy/skills', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'rs1', name: 'Code Review', description: 'Automated code review skill', triggers: null, body: '', active: true, created_at: '2025-06-01T10:00:00Z', updated_at: '2025-06-01T10:00:00Z' }, { id: 'rs2', name: 'Documentation', description: 'Generates documentation from code', triggers: null, body: '', active: false, created_at: '2025-06-02T10:00:00Z', updated_at: '2025-06-02T10:00:00Z' }], total: 2 }) })
    })
    // Context sources
    await page.route('**/api/v1/admin/remy/context-sources', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) })
    })
    // Model backends (for provider status check)
    await page.route('**/api/v1/model-backends**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) })
    })
    // Users and teams
    await page.route('**/api/v1/admin/users**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) })
    })
    await page.route('**/api/v1/admin/teams**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) })
    })

    await page.goto('/admin/remy')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Remy Configuration')
    await expect(page.getByTestId('remy-providers')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('remy-custom-backends')).toBeVisible()
  })
})
