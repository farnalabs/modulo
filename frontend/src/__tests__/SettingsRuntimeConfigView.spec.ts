import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: { items: [], has_drift: false },
      error: undefined,
    }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import SettingsRuntimeConfigView from '../views/SettingsRuntimeConfigView.vue'

const getMock = api.GET as unknown as Mock
const gateStub = { template: '<div><slot /></div>' }

describe('SettingsRuntimeConfigView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsRuntimeConfigView, {
      global: { stubs: { FeatureGate: gateStub } },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Runtime Configuration')
  })

  it('suppresses the error alert for a 402 feature-required response', async () => {
    getMock.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:feature_required',
        title: 'Feature Not Available',
        status: 402,
        detail: 'runtime_config is not available on your plan',
      },
    })
    const wrapper = mount(SettingsRuntimeConfigView, {
      global: { stubs: { FeatureGate: gateStub } },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).not.toContain('is not available on your plan')
    })
    expect(wrapper.text()).not.toContain('Feature Not Available')
  })

  it('shows the error alert for genuine non-402 failures', async () => {
    getMock.mockResolvedValueOnce({
      data: undefined,
      error: { status: 500, detail: 'Internal Server Error' },
    })
    const wrapper = mount(SettingsRuntimeConfigView, {
      global: { stubs: { FeatureGate: gateStub } },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Internal Server Error')
    })
  })
})
