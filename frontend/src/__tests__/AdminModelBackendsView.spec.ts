import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const { mockGet } = vi.hoisted(() => ({
  mockGet: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminModelBackendsView from '../views/AdminModelBackendsView.vue'

describe('AdminModelBackendsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: { items: [] }, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminModelBackendsView, {
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
    expect(wrapper.text()).toContain('Model Backends')
  })

  it('segregates preview model backends into a disclosure section and hides in-dev backends', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'native-1', name: 'native-1', display_name: 'Native Backend', provider: 'anthropic', model_id: 'claude', has_credentials: true, default_params: {}, visibility: 'org', tier: 'native' },
          { id: 'preview-1', name: 'preview-1', display_name: 'Preview Backend', provider: 'openai', model_id: 'gpt', has_credentials: true, default_params: {}, visibility: 'org', tier: 'preview' },
          { id: 'indev-1', name: 'indev-1', display_name: 'InDev Backend', provider: 'openai', model_id: 'gpt', has_credentials: true, default_params: {}, visibility: 'org', tier: 'in_dev' },
        ],
      },
      error: undefined,
    })

    const wrapper = mount(AdminModelBackendsView, {
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

    expect(wrapper.text()).toContain('Native Backend')
    expect(wrapper.text()).not.toContain('InDev Backend')
    expect(wrapper.text()).not.toContain('indev-1')

    const previewSection = wrapper.find('[data-testid="model-backends-preview-section"]')
    expect(previewSection.exists()).toBe(true)
    expect(previewSection.text()).toContain('preview-1')
  })
})
