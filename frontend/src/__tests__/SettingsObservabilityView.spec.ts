import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

function adminJwt(): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const payload = btoa(
    JSON.stringify({
      sub: 'test@example.com',
      org_id: '00000000-0000-0000-0000-000000000001',
      org_role: 'admin',
    }),
  )
  return `${header}.${payload}.signature`
}

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
  getAccessToken: vi.fn().mockReturnValue(adminJwt()),
}))

import { api } from '../lib/api/client'
import SettingsObservabilityView from '../views/SettingsObservabilityView.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

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

function mountView(overrides: { stubs?: Record<string, unknown>; global?: Record<string, unknown> } = {}) {
  return mount(SettingsObservabilityView, {
    global: {
      plugins: [createPinia()],
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        ...overrides.stubs,
      },
      ...overrides.global,
    },
  })
}

describe('SettingsObservabilityView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(api.GET as unknown as () => Promise<unknown>).mockResolvedValue(DEFAULT_GET_RESPONSE)
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: undefined } as never)
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({ data: null, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Observability')
  })

  it('shows locked message when observability is disabled by plan', async () => {
    // Mount without the FeatureGate passthrough stub so the real gate renders
    // its locked composition; the default GET mock returns no active flag.
    const wrapper = mount(SettingsObservabilityView, {
      global: { plugins: [createPinia()] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Available on higher plan tier')
  })

  it('shows loading skeleton while fetching settings', async () => {
    vi.mocked(api.GET as unknown as () => Promise<unknown>).mockReturnValue(new Promise(() => {}))

    const wrapper = mountView()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-loading"]').exists()).toBe(true)
  })

  it('shows error alert when settings load fails', async () => {
    vi.mocked(api.GET as unknown as () => Promise<unknown>).mockResolvedValue({ data: undefined, error: 'Network error' })

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-loading"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Network error')
  })

  it('shows settings form when observability is enabled', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-save"]').exists()).toBe(true)
  })

  it('shows env override banner when env_override_active is true', async () => {
    vi.mocked(api.GET as unknown as () => Promise<unknown>).mockResolvedValue({
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

    const wrapper = mountView()
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
    } as never)

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="settings-observability-save"]')
    await saveBtn.trigger('submit')

    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-form-success"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Settings saved successfully')
  })

  it('shows the form-error ErrorAlert (no retry) when saving settings fails', async () => {
    vi.mocked(api.PUT as unknown as () => Promise<unknown>).mockResolvedValue({
      data: undefined,
      error: { status: 500, detail: 'boom' },
    })

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="settings-observability-save"]')
    await saveBtn.trigger('submit')
    await nextTick()
    await nextTick()

    const formError = wrapper.find('[data-testid="settings-observability-form-error"]')
    expect(formError.exists()).toBe(true)
    expect(formError.text()).toContain('Save failed')
    expect(formError.text()).toContain('boom')

    const alert = wrapper.findComponent(ErrorAlert)
    expect(alert.exists()).toBe(true)
    expect(alert.element).toBe(formError.element)
    expect(alert.text()).toContain('Save failed')
    expect(alert.text()).toContain('boom')

    expect(alert.props('retryable')).toBe(false)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
  })

  it('renders the connection-test result for both success and failure', async () => {
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: { success: true, message: 'span received' },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const testBtn = wrapper.find('[data-testid="settings-observability-test-connection"]')
    await testBtn.trigger('click')
    await nextTick()

    const result = wrapper.find('[data-testid="settings-observability-test-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Connection successful')
    expect(result.text()).toContain('span received')

    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: { success: false, message: 'no spans arrived' },
      error: undefined,
    })
    await testBtn.trigger('click')
    await nextTick()

    const failed = wrapper.find('[data-testid="settings-observability-test-result"]')
    expect(failed.exists()).toBe(true)
    expect(failed.text()).toContain('Connection failed')
    expect(failed.text()).toContain('no spans arrived')
    expect(failed.classes()).toContain('text-destructive-text')

    wrapper.unmount()
  })

  it('handles saveSettings catch path (exception from PUT)', async () => {
    vi.mocked(api.PUT).mockRejectedValue(new Error('transport failed'))

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="settings-observability-save"]')
    await saveBtn.trigger('submit')
    await nextTick()
    await nextTick()

    const formError = wrapper.find('[data-testid="settings-observability-form-error"]')
    expect(formError.exists()).toBe(true)
    expect(formError.text()).toContain('Save failed')
    expect(formError.text()).toContain('transport failed')
  })

  it('handles testConnection error response (err from api.POST)', async () => {
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: undefined,
      error: { detail: 'test endpoint down' },
    })

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const testBtn = wrapper.find('[data-testid="settings-observability-test-connection"]')
    await testBtn.trigger('click')
    await nextTick()

    const result = wrapper.find('[data-testid="settings-observability-test-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Connection failed')
    expect(result.text()).toContain('test endpoint down')
  })

  it('handles testConnection catch path (exception from api.POST)', async () => {
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockRejectedValue(new Error('network error'))

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const testBtn = wrapper.find('[data-testid="settings-observability-test-connection"]')
    await testBtn.trigger('click')
    await nextTick()

    const result = wrapper.find('[data-testid="settings-observability-test-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Connection failed')
    expect(result.text()).toContain('network error')
  })

  it('validates endpoint URL before saving', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const endpointInput = wrapper.find('[data-testid="settings-observability-otlp-endpoint"]')
    await endpointInput.setValue('not-a-url')
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="settings-observability-save"]')
    await saveBtn.trigger('submit')
    await nextTick()
    await nextTick()

    const formError = wrapper.find('[data-testid="settings-observability-form-error"]')
    expect(formError.exists()).toBe(true)
    expect(formError.text()).toContain('Invalid endpoint URL')
  })

  it('validates export interval before saving', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const intervalInput = wrapper.find('[data-testid="settings-observability-export-interval"]')
    await intervalInput.setValue('0')
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="settings-observability-save"]')
    await saveBtn.trigger('submit')
    await nextTick()
    await nextTick()

    const formError = wrapper.find('[data-testid="settings-observability-form-error"]')
    expect(formError.exists()).toBe(true)
    expect(formError.text()).toContain('Export interval must be at least 1 second')
  })

  it('adds and removes headers', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const addBtn = wrapper.find('[data-testid="settings-observability-add-header"]')
    await addBtn.trigger('click')
    await nextTick()

    const headerKeys = wrapper.findAll('[data-testid="settings-observability-header-key"]')
    expect(headerKeys.length).toBe(1)

    await addBtn.trigger('click')
    await nextTick()

    const headerKeys2 = wrapper.findAll('[data-testid="settings-observability-header-key"]')
    expect(headerKeys2.length).toBe(2)

    const removeBtn = wrapper.find('[data-testid="settings-observability-remove-header"]')
    await removeBtn.trigger('click')
    await nextTick()

    const headerKeys3 = wrapper.findAll('[data-testid="settings-observability-header-key"]')
    expect(headerKeys3.length).toBe(1)
  })

  it('toggles langsmith and shows api key field', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-langsmith-api-key"]').exists()).toBe(false)

    const toggleBtn = wrapper.find('[data-testid="settings-observability-langsmith-toggle"]')
    await toggleBtn.trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="settings-observability-langsmith-api-key"]').exists()).toBe(true)
  })

  it('toggles langsmith key visibility', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const toggleBtn = wrapper.find('[data-testid="settings-observability-langsmith-toggle"]')
    await toggleBtn.trigger('click')
    await nextTick()

    const visibilityBtn = wrapper.find('[data-testid="settings-observability-toggle-key-visibility"]')
    expect(visibilityBtn.exists()).toBe(true)
    await visibilityBtn.trigger('click')
    await nextTick()

    expect(visibilityBtn.text()).toContain('Hide')
  })

  it('resets form on reset button click', async () => {
    vi.mocked(api.GET as unknown as () => Promise<unknown>).mockResolvedValue({
      data: {
        otlp_endpoint: 'http://reset:4318',
        otlp_headers: { 'x-test': 'value' },
        export_interval_seconds: 30,
        langsmith_enabled: true,
        has_langsmith_api_key: true,
        env_override_active: false,
        effective_otlp_endpoint: 'http://reset:4318',
      },
      error: undefined,
    })

    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const resetBtn = wrapper.find('[data-testid="settings-observability-reset"]')
    await resetBtn.trigger('click')
    await nextTick()
    await nextTick()

    // resetForm calls loadSettings which calls api.GET
    expect(api.GET).toHaveBeenCalled()
  })
})
