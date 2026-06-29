import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mockLicenseFree = {
  has_license: false,
  tier: 'free',
  features: [],
  expires_at: null,
  org_id: null,
}

const mockLicenseEnterprise = {
  has_license: true,
  tier: 'enterprise',
  features: ['parallel_branches', 'eval_system'],
  expires_at: '2026-12-31T23:59:59Z',
  org_id: 'Acme Corp',
}

const mockFlagsFree = {
  license: { tier: 'free', has_license_key: false, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'enterprise', currently_active: false, depends_on: null },
    { name: 'hitl_gates', description: 'Human-in-the-loop gates', tier: 'free', currently_active: true, depends_on: null },
  ],
  would_activate: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'enterprise', currently_active: false, depends_on: null },
  ],
}

const mockFlagsEnterprise = {
  license: { tier: 'enterprise', has_license_key: true, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'enterprise', currently_active: true, depends_on: null },
    { name: 'hitl_gates', description: 'Human-in-the-loop gates', tier: 'free', currently_active: true, depends_on: null },
  ],
  would_activate: [],
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: { tier: 'enterprise', expires_at: '2027-01-01' }, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SettingsLicenseView from '../views/SettingsLicenseView.vue'

const dialogStubs = ['Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle']

function mockApiResponses(getMock: any, licenseData: any, flagsData: any) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/admin/license') {
      return Promise.resolve({ data: licenseData, error: undefined })
    }
    if (path === '/api/v1/admin/feature-flags') {
      return Promise.resolve({ data: flagsData, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
}

describe('SettingsLicenseView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseFree, mockFlagsFree)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })

  it('shows loading spinner initially', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows error alert on API failure', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: null, error: 'License API error' }))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('License API error')
  })

  it('displays Free Tier content', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseFree, mockFlagsFree)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Free Tier')
    expect(wrapper.text()).toContain('Get an Enterprise License')
  })

  it('displays Enterprise tier content', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseEnterprise, mockFlagsEnterprise)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Enterprise')
    expect(wrapper.text()).toContain('Acme Corp')
    expect(wrapper.text()).toContain('December')
    expect(wrapper.text()).toContain('Enterprise license key active')
  })

  it('renders feature flags list with enabled/disabled states', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseEnterprise, mockFlagsEnterprise)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('parallel_branches')
    expect(wrapper.text()).toContain('hitl_gates')
    expect(wrapper.text()).toContain('Run parallel branches')
    expect(wrapper.text()).toContain('2 of 2 features active')
  })

  it('renders license key textarea and action buttons', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseEnterprise, mockFlagsEnterprise)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.text()).toContain('Verify Key')
    expect(wrapper.text()).toContain('Apply Key')
  })
})
