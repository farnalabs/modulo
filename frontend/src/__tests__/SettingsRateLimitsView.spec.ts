import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: { mode: 'in_memory', rules: [] },
      error: undefined,
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import SettingsRateLimitsView from '../views/SettingsRateLimitsView.vue'

const getMock = api.GET as unknown as Mock
const gateStub = { template: '<div><slot /></div>' }

describe('SettingsRateLimitsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsRateLimitsView, {
      global: { stubs: { FeatureGate: gateStub } },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Rate Limits')
  })

  it('suppresses the error alert for a 402 feature-required response', async () => {
    getMock.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:feature_required',
        title: 'Feature Not Available',
        status: 402,
        detail: 'rate_limits is not available on your plan',
      },
    })
    const wrapper = mount(SettingsRateLimitsView, {
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
    const wrapper = mount(SettingsRateLimitsView, {
      global: { stubs: { FeatureGate: gateStub } },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Internal Server Error')
    })
  })
})
