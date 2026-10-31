import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api/client'

const mockFlagsData = {
  license: { tier: 'community', has_license_key: false, is_valid: true },
  flags: [
    { name: 'flag-connectors', description: 'Third-party connector support', tier: 'community', currently_active: true, depends_on: null },
    { name: 'flag-audit-log', description: 'Audit log retention', tier: 'team', currently_active: false, depends_on: null },
    { name: 'flag-advanced-analytics', description: 'Advanced analytics dashboards', tier: 'team', currently_active: false, depends_on: ['flag-audit-log'] },
  ],
  would_activate: [
    { name: 'flag-audit-log', description: 'Audit log retention', tier: 'team', depends_on: null },
    { name: 'flag-advanced-analytics', description: 'Advanced analytics dashboards', tier: 'team', depends_on: ['flag-audit-log'] },
  ],
}

const mockTiersData = {
  tiers: [
    { tier_id: 'community', label: 'Community', rank: 0 },
    { tier_id: 'team', label: 'Team', rank: 1 },
  ],
}

const mockLicenseData = {
  expires_at: null,
  org_id: null,
  tier: 'community',
}

const mockPlanData = {
  license: { tier: 'community', expires_at: null, org_id: null },
  dev_mode: false,
  flags: [
    { name: 'flag-connectors', currently_active: true },
    { name: 'flag-audit-log', currently_active: false },
  ],
}

function setupDefaultMock() {
  api.GET = vi.fn((path: string) => {
    if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
      return Promise.resolve({ data: { override: null }, error: undefined })
    }
    if (path === '/api/v1/plan') {
      return Promise.resolve({ data: mockPlanData, error: undefined })
    }
    if (path === '/api/v1/admin/feature-flags') {
      return Promise.resolve({ data: mockFlagsData, error: undefined })
    }
    if (path === '/api/v1/admin/license') {
      return Promise.resolve({ data: mockLicenseData, error: undefined })
    }
    if (path === '/api/v1/admin/tiers') {
      return Promise.resolve({ data: mockTiersData, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  }) as unknown as typeof api.GET
}

async function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  setupDefaultMock()
  const wrapper = mount(AdminFeatureFlagsView, {
    global: { plugins: [pinia] },
  })
  for (let i = 0; i < 10; i++) {
    await flushPromises()
  }
  return wrapper
}

import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'

describe('AdminFeatureFlagsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMock()
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Feature Flags')
  })

  it('renders the search input', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[data-testid="filter-bar-search"]')
    expect(input.exists()).toBe(true)
  })

  it('filters flags by search query', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[data-testid="filter-bar-search"]')
    await input.setValue('audit')
    expect(wrapper.text()).toContain('flag-audit-log')
    expect(wrapper.text()).not.toContain('flag-connectors')
  })

  it('groups flags by tier with section headers', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('community')
    expect(wrapper.text()).toContain('team')
  })

  it('shows toggle indicators for each flag', async () => {
    const wrapper = await mountView()
    const toggles = wrapper.findAll('[role="switch"]')
    expect(toggles.length).toBe(4)
  })

  it('shows active toggle for active flags', async () => {
    const wrapper = await mountView()
    const flagToggles = wrapper.findAll('table [role="switch"]')
    const activeToggle = flagToggles[0]
    expect(activeToggle.classes()).toContain('bg-primary')
  })

  it('shows inactive toggle for inactive flags', async () => {
    const wrapper = await mountView()
    const flagToggles = wrapper.findAll('table [role="switch"]')
    const inactiveToggle = flagToggles[1]
    expect(inactiveToggle.classes()).toContain('bg-input')
  })

  it('renders the license status card', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('License Status')
    expect(wrapper.text()).toContain('community')
    expect(wrapper.text()).toContain('Not set')
    expect(wrapper.text()).toContain('Valid')
  })

  it('renders the "Would activate" section for enterprise-tier flags', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Would activate with a license key')
    expect(wrapper.text()).toContain('flag-audit-log')
    expect(wrapper.text()).toContain('flag-advanced-analytics')
  })

  it('shows count per tier section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toMatch(/[Cc]ommunity/i)
    expect(wrapper.text()).toMatch(/[Tt]eam/i)
  })

  it('shows a loading skeleton, not an indeterminate spinner, while fetching', async () => {
    api.GET = vi.fn().mockReturnValue(new Promise(() => {}))
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: { plugins: [pinia] },
    })
    const header = wrapper.find('header')
    expect(header.find('.animate-pulse').exists()).toBe(true)
    expect(header.find('.animate-spin').exists()).toBe(false)
    const status = header.find('output')
    expect(status.exists()).toBe(true)
    expect(status.attributes('aria-busy')).toBe('true')
    expect(status.text()).toContain('Loading plan info')
  })

  it('makes the flag-name tooltip trigger keyboard-focusable', async () => {
    const wrapper = await mountView()
    const flagName = wrapper.find('[data-testid="flag-name-flag-connectors"]')
    expect(flagName.exists()).toBe(true)
    expect(flagName.element.tagName).toBe('BUTTON')
    expect(flagName.attributes('type')).toBe('button')
    expect(flagName.attributes('tabindex')).toBeUndefined()
  })

  it('shows error message with retry on API error', async () => {
    api.GET = vi.fn().mockResolvedValue({ data: null, error: 'Network failure' })
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: { plugins: [pinia] },
    })
    for (let i = 0; i < 5; i++) {
      await flushPromises()
    }
    expect(wrapper.text()).toContain('Network failure')
  })

  it('shows empty state when search yields no results', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[data-testid="filter-bar-search"]')
    await input.setValue('zzz_no_match_zzz')
    expect(wrapper.text()).toContain('No feature flags match your search')
  })

  it('shows pagination when flags exceed page size', async () => {
    const manyFlags = Array.from({ length: 25 }, (_, i) => ({
      name: `flag-${i}`,
      description: `Flag number ${i}`,
      tier: i < 10 ? 'community' : i < 20 ? 'team' : 'v1',
      currently_active: i < 5,
      depends_on: null,
    }))
    const mockManyFlags = { ...mockFlagsData, flags: manyFlags, would_activate: manyFlags.filter(f => !f.currently_active) }

    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: null }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: mockManyFlags, error: undefined })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: { plugins: [pinia] },
    })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    expect(wrapper.text()).toContain('Next')
    expect(wrapper.text()).toContain('Previous')
  })

  it('shows tooltip trigger elements', async () => {
    const wrapper = await mountView()
    const triggers = wrapper.findAll('.cursor-help')
    expect(triggers.length).toBe(3)
    expect(triggers[0].text()).toBe('flag-connectors')
  })

  it('locks flags whose tier outranks the current tier, not only "team"', async () => {
    const flagsData = {
      license: { tier: 'community', has_license_key: false, is_valid: true },
      flags: [
        { name: 'flag-v1', description: 'V1 feature', tier: 'v1', currently_active: false, depends_on: null },
      ],
      would_activate: [],
    }
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: null }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: flagsData, error: undefined })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({
          data: {
            tiers: [
              { tier_id: 'community', label: 'Community', rank: 0 },
              { tier_id: 'team', label: 'Team', rank: 1 },
              { tier_id: 'v1', label: 'V1', rank: 2 },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, { global: { plugins: [pinia] } })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    const toggle = wrapper.find('[data-testid="flag-toggle-flag-v1"]')
    expect(toggle.attributes('aria-disabled')).toBe('true')
  })

  it('renders the loaded plan info in the header once the plan payload lands', async () => {
    const wrapper = await mountView()
    const header = wrapper.find('header')
    expect(header.find('output').exists()).toBe(false)
    expect(header.text()).toContain('Plan:')
    expect(header.text()).toContain('Features enabled:')
    expect(header.text()).toContain('community')
  })

  it('shows an inline plan-unavailable message with retry instead of a fabricated plan', async () => {
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: null }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: mockFlagsData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: 'plan unavailable' })
    }) as unknown as typeof api.GET
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, { global: { plugins: [pinia] } })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    const header = wrapper.find('header')
    expect(header.text()).toContain('Plan information could not be loaded.')
    expect(header.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
  })

  it('surfaces a dev-mode toggle failure with a retry for the requested value', async () => {
    const wrapper = await mountView()
    const put = vi.fn()
      .mockResolvedValueOnce({ data: null, error: 'not allowed' })
      .mockResolvedValueOnce({ data: { enabled: true }, error: undefined })
    api.PUT = put as unknown as typeof api.PUT

    await wrapper.find('[data-testid="developer-mode-toggle"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to update developer mode:')
    expect(put).toHaveBeenCalledWith('/api/v1/admin/dev-mode', { body: { enabled: true } })
    expect(wrapper.find('[data-testid="developer-mode-toggle"]').attributes('aria-checked')).toBe('false')

    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    for (let i = 0; i < 5; i++) {
      await flushPromises()
    }
    expect(put).toHaveBeenCalledTimes(2)
    expect(put).toHaveBeenLastCalledWith('/api/v1/admin/dev-mode', { body: { enabled: true } })
    expect(wrapper.find('[data-testid="developer-mode-toggle"]').attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).not.toContain('Failed to update developer mode:')
  })

  it('clamps back to the last page when a refetch shrinks the result set', async () => {
    const manyFlags = Array.from({ length: 25 }, (_, i) => ({
      name: `flag-${i}`,
      description: `Flag number ${i}`,
      tier: 'community',
      currently_active: i < 5,
      depends_on: null,
    }))
    let currentFlags = manyFlags
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: null }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: { ...mockFlagsData, flags: currentFlags, would_activate: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/plan') {
        return Promise.resolve({ data: mockPlanData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    api.PUT = vi.fn().mockResolvedValue({ data: { enabled: false }, error: undefined }) as unknown as typeof api.PUT

    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, { global: { plugins: [pinia] } })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    await wrapper.find('[data-testid="flags-next-page"]').trigger('click')
    await wrapper.find('[data-testid="flags-next-page"]').trigger('click')
    expect(wrapper.text()).toContain('Page 3 of 3')

    currentFlags = manyFlags.slice(0, 3)
    await wrapper.find('[data-testid="flag-toggle-flag-20"]').trigger('click')
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }

    expect(wrapper.text()).not.toContain('Page 3 of 3')
    expect(wrapper.find('[data-testid="flags-next-page"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('flag-0')
  })

  it('allows disabling an active locked flag (backend only blocks enabling)', async () => {
    const flagsData = {
      license: { tier: 'community', has_license_key: false, is_valid: true },
      flags: [
        { name: 'flag-audit-log', description: 'Audit log retention', tier: 'team', currently_active: true, depends_on: null },
      ],
      would_activate: [],
    }
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: null }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: flagsData, error: undefined })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    const putMock = vi.fn().mockResolvedValue({ data: {}, error: undefined })
    api.PUT = putMock as unknown as typeof api.PUT
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, { global: { plugins: [pinia] } })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    const toggle = wrapper.find('[data-testid="flag-toggle-flag-audit-log"]')
    expect(toggle.attributes('aria-disabled')).toBeUndefined()
    await toggle.trigger('click')
    await flushPromises()
    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/admin/feature-flags/{flag_name}',
      expect.objectContaining({ body: { enabled: false } }),
    )
  })

  it('does not offer or preselect Force enabled for a locked-but-active flag', async () => {
    const flagsData = {
      license: { tier: 'community', has_license_key: false, is_valid: true },
      flags: [
        { name: 'flag-audit-log', description: 'Audit log retention', tier: 'team', currently_active: true, depends_on: null },
      ],
      would_activate: [],
    }
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        // The org previously forced this locked flag on; the dialog must not
        // reopen with the (removed) 'true' option selected.
        return Promise.resolve({ data: { override: true }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: flagsData, error: undefined })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: {
        plugins: [pinia],
        stubs: { FormDialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' } },
      },
    })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    await wrapper.find('[data-testid="flag-override-flag-audit-log"]').trigger('click')
    await flushPromises()
    const select = wrapper.find('[data-testid="flag-override-select"]')
    expect(select.exists()).toBe(true)
    expect(select.text()).not.toContain('Force enabled')
    expect(select.text()).toContain('System default')
  })

  it('preselects Force disabled when the stored org override is false', async () => {
    const flagsData = {
      license: { tier: 'community', has_license_key: false, is_valid: true },
      flags: [
        { name: 'flag-connectors', description: 'Third-party connector support', tier: 'community', currently_active: true, depends_on: null },
      ],
      would_activate: [],
    }
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: false }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: flagsData, error: undefined })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: {
        plugins: [pinia],
        stubs: { FormDialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' } },
      },
    })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    await wrapper.find('[data-testid="flag-override-flag-connectors"]').trigger('click')
    await flushPromises()
    const select = wrapper.find('[data-testid="flag-override-select"]')
    expect(select.exists()).toBe(true)
    expect(select.text()).toContain('Force disabled')
  })

  it('preselects System default when the flag has no stored org override', async () => {
    const flagsData = {
      license: { tier: 'community', has_license_key: false, is_valid: true },
      flags: [
        { name: 'flag-connectors', description: 'Third-party connector support', tier: 'community', currently_active: true, depends_on: null },
      ],
      would_activate: [],
    }
    api.GET = vi.fn((path: string) => {
      if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
        return Promise.resolve({ data: { override: null }, error: undefined })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: flagsData, error: undefined })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseData, error: undefined })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersData, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }) as unknown as typeof api.GET
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: {
        plugins: [pinia],
        stubs: { FormDialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' } },
      },
    })
    for (let i = 0; i < 10; i++) {
      await flushPromises()
    }
    await wrapper.find('[data-testid="flag-override-flag-connectors"]').trigger('click')
    await flushPromises()
    const select = wrapper.find('[data-testid="flag-override-select"]')
    expect(select.exists()).toBe(true)
    expect(select.text()).toContain('System default')
    expect(select.text()).not.toContain('Force disabled')
  })
})
