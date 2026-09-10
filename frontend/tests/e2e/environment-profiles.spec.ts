import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Runners (Profiles tab, legacy environment-profiles routes)', { tag: "@regression" }, () => {
  test('renders the Runners page from the legacy /environment-profiles route', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    if (env.name === 'local') {
      await page.route('**/api/v1/environment-profiles', (route) => {
        if (route.request().method() !== 'GET') return route.fallback()
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [mockProfileSummary], total: 1 }),
        })
      })
      await page.route('**/api/v1/runners/status', (route) => {
        if (route.request().method() !== 'GET') return route.fallback()
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(mockRunnersStatus),
        })
      })
    }
    await page.goto('/environment-profiles')
    await expect(page).toHaveURL(/\/admin\/runners\/profiles$/)
    await expect(page.getByRole('heading', { name: 'Runners' })).toContainText('Runners')
    if (env.name === 'local') {
      await expect(page.getByTestId('envprofile-list-new')).toBeVisible()
      await expect(page.getByTestId('runner-status-strip')).toBeVisible()
    }
  })

  test('redirects the retired /admin/environments path', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/environments')
    await expect(page).toHaveURL(/\/admin\/runners\/profiles$/)
    await expect(page.getByRole('heading', { name: 'Runners' })).toContainText('Runners')
  })

  test('renders the new-profile form', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/runners/profiles/new')
    await expect(page).toHaveURL(/\/admin\/runners\/profiles\/new$/)
    await expect(page.getByRole('heading', { name: 'New Environment Profile' })).toContainText('New Environment Profile')
    if (env.name === 'local') {
      await expect(page.getByTestId('envprofile-form-name')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-description')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-provider')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-image')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-network')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-init')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-persistence')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-submit')).toBeVisible()
    }
  })

  test('shows a validation error on empty submit and cancel returns to the profiles tab', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/runners/profiles/new')
    await expect(page.getByRole('heading', { name: 'New Environment Profile' })).toContainText('New Environment Profile')
    await page.getByTestId('envprofile-form-submit').click()
    await expect(page.getByText('Name is required')).toBeVisible()
    await page.getByTestId('envprofile-form-cancel').click()
    await expect(page).toHaveURL(/\/admin\/runners\/profiles$/)
    await expect(page.getByRole('heading', { name: 'Runners' })).toContainText('Runners')
  })

  test('renders the edit-profile form for an unknown profile id', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    if (env.name === 'local') {
      await page.route('**/api/v1/environment-profiles/e2e-missing-profile', (route) => {
        route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ type: 'urn:problem:modulo:not_found', title: 'Not Found', status: 404, detail: 'Profile not found' }),
        })
      })
    }
    await page.goto('/admin/runners/profiles/e2e-missing-profile/edit')
    await expect(page).toHaveURL(/\/admin\/runners\/profiles\/e2e-missing-profile\/edit$/)
    await expect(page.getByRole('heading', { name: 'Edit Environment Profile' })).toContainText('Edit Environment Profile')
    if (env.name === 'local') {
      await expect(page.getByTestId('envprofile-form-name')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-submit')).toBeVisible()
      await expect(page.getByTestId('envprofile-form-cancel')).toBeVisible()
    }
  })

  test('legacy profile deep links land on the new editor routes', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/environment-profiles/e2e-missing-profile/edit')
    await expect(page).toHaveURL(/\/admin\/runners\/profiles\/e2e-missing-profile\/edit$/)
    await expect(page.getByRole('heading', { name: 'Edit Environment Profile' })).toContainText('Edit Environment Profile')
  })

  test('navigates from the list into a prefilled edit form', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi data — only runs locally')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/environment-profiles', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [mockProfileSummary], total: 1 }),
      })
    })
    await page.route('**/api/v1/runners/status', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockRunnersStatus),
      })
    })
    await page.route('**/api/v1/environment-profiles/e2e-profile-1', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockProfileDetail),
      })
    })
    await page.goto('/admin/runners/profiles')
    await expect(page.getByTestId('envprofile-list-edit')).toBeVisible()
    await page.getByTestId('envprofile-list-edit').click()
    await expect(page).toHaveURL(/\/admin\/runners\/profiles\/e2e-profile-1\/edit$/)
    await expect(page.getByRole('heading', { name: 'Edit Environment Profile' })).toContainText('Edit Environment Profile')
    await expect(page.getByTestId('envprofile-form-name')).toHaveValue('e2e-edit-profile')
  })
})

const mockProfileSummary = {
  id: 'e2e-profile-1',
  name: 'e2e-edit-profile',
  description: 'Profile used by the e2e edit-form spec',
  provider_type: 'local_docker',
  image_ref: 'python:3.12-slim',
  capabilities: ['git', 'shell'],
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
}

const mockProfileDetail = {
  ...mockProfileSummary,
  network_policy: 'outbound',
  initialisation_strategy: 'git_clone',
  persistence_policy: 'ephemeral',
  updated_at: '2026-01-01T00:00:00Z',
}

const mockRunnersStatus = {
  aggregate_state: 'healthy',
  probe_interval_seconds: 60,
  staleness_threshold_seconds: 120,
  machines: [
    {
      machine_id: 'machine-1',
      state: 'healthy',
      engine_reachable: true,
      images_present: true,
      probed_at: '2026-01-01T00:00:00Z',
      age_seconds: 20,
      engine_info: { cpu_count: 8, mem_total_mb: 16384 },
      image_checks: {},
      probe_error: null,
    },
  ],
  profiles: [
    {
      id: 'e2e-profile-1',
      name: 'e2e-edit-profile',
      description: 'Profile used by the e2e edit-form spec',
      provider_type: 'local_docker',
      image_ref: 'python:3.12-slim',
      config_json: {},
      network_policy: 'outbound',
      persistence_policy: 'ephemeral',
      status: 'active',
      health_state: null,
      available: true,
      placeholder_digest: false,
      drift: { is_seeded: false, drifted: false, drifted_fields: [] },
    },
  ],
  concurrency: {
    sandbox_concurrency_limit: 4,
    is_default: true,
    preflight: {
      state: 'ok',
      detail: null,
      engine_cpu_count: 8,
      engine_mem_total_mb: 16384,
      needed_cpu: 4,
      needed_mem_mb: 4096,
    },
  },
}
