import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { mockGet } = vi.hoisted(() => ({
  mockGet: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminConnectorsView from '../views/AdminConnectorsView.vue'

describe('AdminConnectorsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: { items: [] }, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminConnectorsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Connectors')
  })

  it('segregates preview connectors into a disclosure section and hides in-dev connectors', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'native-1', name: 'Native Connector', connector_type: 'postgresql', description: null, tier: 'native' },
          { id: 'preview-1', name: 'Preview Connector', connector_type: 'http', description: null, tier: 'preview' },
          { id: 'indev-1', name: 'InDev Connector', connector_type: 'http', description: null, tier: 'in_dev' },
        ],
      },
      error: undefined,
    })

    const wrapper = mount(AdminConnectorsView, {
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

    expect(wrapper.text()).toContain('Native Connector')
    expect(wrapper.text()).not.toContain('InDev Connector')

    const previewSection = wrapper.find('[data-testid="connectors-preview-section"]')
    expect(previewSection.exists()).toBe(true)
    expect(previewSection.text()).toContain('Preview Connector')
  })
})
