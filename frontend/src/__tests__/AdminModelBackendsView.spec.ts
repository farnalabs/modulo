import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
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
})
