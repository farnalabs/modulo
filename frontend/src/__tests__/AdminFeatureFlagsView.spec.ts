import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: {
        license: { tier: 'free', has_license_key: false, is_valid: true },
        flags: [
          { name: 'flag-connectors', description: 'Third-party connector support', tier: 'free', currently_active: true, depends_on: null },
          { name: 'flag-audit-log', description: 'Audit log retention', tier: 'enterprise', currently_active: false, depends_on: null },
          { name: 'flag-advanced-analytics', description: 'Advanced analytics dashboards', tier: 'enterprise', currently_active: false, depends_on: ['flag-audit-log'] },
        ],
        would_activate: [
          { name: 'flag-audit-log', description: 'Audit log retention', tier: 'enterprise', depends_on: null },
          { name: 'flag-advanced-analytics', description: 'Advanced analytics dashboards', tier: 'enterprise', depends_on: ['flag-audit-log'] },
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
    expect(wrapper.text()).toContain('Free')
    expect(wrapper.text()).toContain('Enterprise')
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
    expect(wrapper.text()).toContain('free')
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
    const freeSection = sections.find(s => s.text().includes('Free'))
    const enterpriseSection = sections.find(s => s.text().includes('Enterprise'))
    expect(freeSection).toBeDefined()
    expect(enterpriseSection).toBeDefined()
  })

  it('shows tooltip trigger elements', async () => {
    const wrapper = await mountView()
    const triggers = wrapper.findAll('.cursor-help')
    expect(triggers.length).toBe(3)
    expect(triggers[0].text()).toBe('flag-connectors')
  })
})
