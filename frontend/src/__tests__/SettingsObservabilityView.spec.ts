import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: {
        otlp_endpoint: '',
        otlp_headers: {},
        export_interval_seconds: 10,
        langsmith_enabled: false,
        has_langsmith_api_key: false,
        env_override_active: false,
        effective_otlp_endpoint: '',
      },
      error: undefined,
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SettingsObservabilityView from '../views/SettingsObservabilityView.vue'

describe('SettingsObservabilityView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsObservabilityView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Observability')
  })
})
