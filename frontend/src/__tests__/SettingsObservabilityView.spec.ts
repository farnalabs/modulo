import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
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

import { api } from '../lib/api/client'
import SettingsObservabilityView from '../views/SettingsObservabilityView.vue'

const DEFAULT_GET_RESPONSE = {
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
}

describe('SettingsObservabilityView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(api.GET).mockResolvedValue(DEFAULT_GET_RESPONSE)
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: undefined })
    vi.mocked(api.POST).mockResolvedValue({ data: null, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsObservabilityView, {
      global: { plugins: [createPinia()] },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Observability')
  })

  it('shows loading spinner while fetching settings', async () => {
    vi.mocked(api.GET).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsObservabilityView, {
      global: {
        plugins: [createPinia()],
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
      },
    })
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-loading"]').exists()).toBe(true)
  })

  it('shows error alert when settings load fails', async () => {
    vi.mocked(api.GET).mockResolvedValue({ data: undefined, error: 'Network error' })

    const wrapper = mount(SettingsObservabilityView, {
      global: {
        plugins: [createPinia()],
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
      },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-loading"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Failed to load settings')
  })

  it('shows locked message when observability is disabled by plan', async () => {
    const wrapper = mount(SettingsObservabilityView, {
      global: { plugins: [createPinia()] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Available on higher plan tier')
  })

  it('shows settings form when observability is enabled', async () => {
    const wrapper = mount(SettingsObservabilityView, {
      global: {
        plugins: [createPinia()],
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
      },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-save"]').exists()).toBe(true)
  })



  it('shows env override banner when env_override_active is true', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: {
        otlp_endpoint: '',
        otlp_headers: {},
        export_interval_seconds: 10,
        langsmith_enabled: false,
        has_langsmith_api_key: false,
        env_override_active: true,
        effective_otlp_endpoint: 'http://env:4318',
      },
      error: undefined,
    })

    const wrapper = mount(SettingsObservabilityView, {
      global: {
        plugins: [createPinia()],
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
      },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-env-override"]').exists()).toBe(true)
  })

  it('shows success message after saving settings', async () => {
    vi.mocked(api.PUT).mockResolvedValue({
      data: {
        otlp_endpoint: 'http://e:4318',
        otlp_headers: {},
        export_interval_seconds: 10,
        langsmith_enabled: false,
        has_langsmith_api_key: false,
        env_override_active: false,
        effective_otlp_endpoint: 'http://e:4318',
      },
      error: undefined,
    })

    const wrapper = mount(SettingsObservabilityView, {
      global: {
        plugins: [createPinia()],
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
      },
    })
    await nextTick()
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="settings-observability-save"]')
    await saveBtn.trigger('submit')

    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-form-success"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Settings saved successfully')
  })
})
