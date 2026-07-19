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

function setupDefaultMock() {
  api.GET = vi.fn((path: string) => {
    if (path.startsWith('/api/v1/admin/feature-flags') && path.includes('org-override')) {
      return Promise.resolve({ data: { override: null }, error: undefined })
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
    const input = wrapper.find('input[placeholder*="Search flags"]')
    expect(input.exists()).toBe(true)
  })

  it('filters flags by search query', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[placeholder*="Search flags"]')
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
    expect(toggles.length).toBe(3)
  })

  it('shows active toggle for active flags', async () => {
    const wrapper = await mountView()
    const toggles = wrapper.findAll('[role="switch"]')
    const activeToggle = toggles[0]
    expect(activeToggle.classes()).toContain('bg-primary')
  })

  it('shows inactive toggle for inactive flags', async () => {
    const wrapper = await mountView()
    const toggles = wrapper.findAll('[role="switch"]')
    const inactiveToggle = toggles[1]
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

  it('shows loading spinner while fetching', async () => {
    api.GET = vi.fn().mockReturnValue(new Promise(() => {}))
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AdminFeatureFlagsView, {
      global: { plugins: [pinia] },
    })
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
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
    const input = wrapper.find('input[placeholder*="Search flags"]')
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
})
