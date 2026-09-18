import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: [], error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminPluginsView from '../views/AdminPluginsView.vue'

const mockPlugins = [
  {
    PLUGIN_ID: 'modulo-xyz',
    display_name: 'XYZ Connector',
    description: 'A test plugin',
    version: '1.0.0',
    capabilities: ['connector_type'],
    health_ok: true,
    health_detail: 'Loaded',
    health_checked_at: '2025-01-01T00:00:00Z',
  },
]

describe('AdminPluginsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminPluginsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Plugins')
  })

  it('displays plugins from API response', async () => {
    const { api: mockApi } = await import('../lib/api/client')
    ;(mockApi.GET as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: mockPlugins,
      error: undefined,
    })

    const wrapper = mount(AdminPluginsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('XYZ Connector')
    expect(wrapper.text()).toContain('1.0.0')
    expect(wrapper.text()).toContain('connector')
  })

  it('shows empty state when no plugins', async () => {
    const { api: mockApi } = await import('../lib/api/client')
    ;(mockApi.GET as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: [],
      error: undefined,
    })

    const wrapper = mount(AdminPluginsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('No plugins installed')
  })
})
