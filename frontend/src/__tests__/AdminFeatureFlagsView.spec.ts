import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: {
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
      },
      error: undefined,
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'

async function mountView() {
  const pinia = createPinia()
  const wrapper = mount(AdminFeatureFlagsView, {
    global: { plugins: [pinia] },
  })
  for (let i = 0; i < 5; i++) {
    await nextTick()
  }
  return wrapper
}

describe('AdminFeatureFlagsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Feature Flags')
  })

  it('renders the search input', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[placeholder="Search flags by name or description..."]')
    expect(input.exists()).toBe(true)
  })

  it('filters flags by search query', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[placeholder="Search flags by name or description..."]')
    await input.setValue('audit')
    await nextTick()
    expect(wrapper.text()).toContain('flag-audit-log')
    expect(wrapper.text()).not.toContain('flag-connectors')
  })

  it('groups flags by tier with section headers', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Community')
    expect(wrapper.text()).toContain('Team')
  })

  it('shows toggle indicators for each flag', async () => {
    const wrapper = await mountView()
    const toggles = wrapper.findAll('.inline-flex.h-5.w-9')
    expect(toggles.length).toBe(3)
  })

  it('shows active toggle for active flags', async () => {
    const wrapper = await mountView()
    const toggles = wrapper.findAll('.inline-flex.h-5.w-9')
    const activeToggle = toggles[0]
    expect(activeToggle.classes()).toContain('bg-primary')
  })

  it('shows inactive toggle for inactive flags', async () => {
    const wrapper = await mountView()
    const toggles = wrapper.findAll('.inline-flex.h-5.w-9')
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
    const sections = wrapper.findAll('.uppercase.tracking-wider.text-muted-foreground')
    const communitySection = sections.find(s => s.text().includes('Community'))
    const teamSection = sections.find(s => s.text().includes('Team'))
    expect(communitySection).toBeDefined()
    expect(teamSection).toBeDefined()
  })

  it('shows loading spinner while fetching', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockReturnValue(new Promise(() => {})) // never resolves
    const wrapper = await mountView()
    const spinner = wrapper.find('.animate-spin')
    expect(spinner.exists()).toBe(true)
  })

  it('shows error message with retry on API error', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: null, error: 'Network failure' })
    const wrapper = await mountView()
    for (let i = 0; i < 5; i++) {
      await nextTick()
    }
    expect(wrapper.text()).toContain('Failed to load feature flags')
  })

  it('shows empty state when search yields no results', async () => {
    const wrapper = await mountView()
    const input = wrapper.find('input[placeholder="Search flags by name or description..."]')
    await input.setValue('zzz_no_match_zzz')
    await nextTick()
    expect(wrapper.text()).toContain('No feature flags match your search')
  })

  it('shows pagination when flags exceed page size', async () => {
    const { api } = await import('../lib/api/client')
    const manyFlags = Array.from({ length: 25 }, (_, i) => ({
      name: `flag-${i}`,
      description: `Flag number ${i}`,
      tier: i < 10 ? 'community' : i < 20 ? 'team' : 'v1',
      currently_active: i < 5,
      depends_on: null,
    }))
    ;(api.GET as any).mockResolvedValue({
      data: {
        license: { tier: 'community', has_license_key: false, is_valid: true },
        flags: manyFlags,
        would_activate: manyFlags.filter(f => !f.currently_active),
      },
      error: undefined,
    })
    const wrapper = await mountView()
    for (let i = 0; i < 5; i++) {
      await nextTick()
    }
    const pageText = wrapper.text()
    expect(pageText).toContain('Next')
    expect(pageText).toContain('Previous')
  })

  it('shows tooltip trigger elements', async () => {
    const wrapper = await mountView()
    const triggers = wrapper.findAll('.cursor-help')
    expect(triggers.length).toBe(3)
    expect(triggers[0].text()).toBe('flag-connectors')
  })
})
