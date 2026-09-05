import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'

type ApiResult = { data?: unknown; error?: Record<string, unknown> | undefined }

let mockForwarders: Array<Record<string, unknown>> = []
let mockGetError: Record<string, unknown> | undefined
let mockPutError: Record<string, unknown> | undefined
let mockTestResult: { ok: boolean; message: string } | null
let mockTestError: Record<string, unknown> | undefined
let mockTestThrows: unknown = null

function apiResult(data: unknown, error?: Record<string, unknown>): ApiResult {
  return { data, error }
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/errors/forwarders') {
        if (mockGetError) return Promise.resolve(apiResult(null, mockGetError))
        return Promise.resolve(apiResult({ forwarders: mockForwarders }))
      }
      return Promise.resolve(apiResult(null))
    }),
    PUT: vi.fn().mockImplementation(() => {
      if (mockPutError) return Promise.resolve(apiResult(null, mockPutError))
      return Promise.resolve(apiResult({}))
    }),
    POST: vi.fn().mockImplementation(() => {
      if (mockTestThrows) return Promise.reject(mockTestThrows)
      if (mockTestError) return Promise.resolve(apiResult(null, mockTestError))
      return Promise.resolve(apiResult(mockTestResult))
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SettingsErrorForwardersView from '../views/SettingsErrorForwardersView.vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'

function forwarder(type: string, overrides: Record<string, unknown> = {}) {
  return {
    forwarder_type: type,
    display_name: type.charAt(0).toUpperCase() + type.slice(1),
    enabled: false,
    configured: false,
    last_test_at: null,
    last_test_ok: null,
    ...overrides,
  }
}

function mountView() {
  return mount(SettingsErrorForwardersView)
}

function cardFor(wrapper: ReturnType<typeof mount>, name: string) {
  const cards = wrapper.findAll('.rounded-lg.border.bg-card')
  return cards.find((c) => c.text().includes(name))!
}

function findButton(scope: { findAll: (sel: string) => { text: () => string; trigger: (e: string) => Promise<void> }[] }, label: string | RegExp) {
  return scope.findAll('button').find((b) => (typeof label === 'string' ? b.text().includes(label) : label.test(b.text())))
}

describe('SettingsErrorForwardersView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    usePlanStore().currentTier = 'team'
    mockForwarders = []
    mockGetError = undefined
    mockPutError = undefined
    mockTestResult = null
    mockTestError = undefined
    mockTestThrows = null
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows the loading spinner before the list resolves', async () => {
    // Hold the forwarders GET open so `loading` stays true.
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(() => {}))
    const wrapper = mountView()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
    wrapper.unmount()
  })

  it('surfaces the load failure inline (retry button hidden — repo-wide ErrorAlert bug, see report)', async () => {
    mockGetError = { detail: 'forwarders_disabled' }
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('forwarders_disabled')
    // BUG CHARACTERISATION (repo-wide, mirrors ParameterSchemasView.spec.ts):
    // the ErrorAlert retry button is hidden — `:on-retry="..."` does not
    // reach the child's `onRetry` prop, so `v-if="onRetry && ..."` stays
    // false. No recovery path is offered from this screen.
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(0)
    wrapper.unmount()
  })

  it('renders the forwarder list with connection status and config badges', async () => {
    mockForwarders = [
      forwarder('sentry', { enabled: true, configured: false, last_test_ok: true }),
      forwarder('datadog', { enabled: true, configured: true, last_test_ok: false }),
      forwarder('pagerduty'),
      forwarder('rollbar'),
      forwarder('opsgenie'),
      forwarder('loki'),
    ]
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('Error Forwarders')
    for (const name of ['Sentry', 'Datadog', 'Pagerduty', 'Rollbar', 'Opsgenie', 'Loki']) {
      expect(wrapper.text()).toContain(name)
    }
    const sentryCard = cardFor(wrapper, 'Sentry')
    expect(sentryCard.text()).toContain('Connected')
    const datadogCard = cardFor(wrapper, 'Datadog')
    expect(datadogCard.text()).toContain('Failed')
    const pagerdutyCard = cardFor(wrapper, 'Pagerduty')
    expect(pagerdutyCard.text()).toContain('Not tested')
    expect(pagerdutyCard.text()).toContain('Not configured')
    wrapper.unmount()
  })

  it('auto-expands configured forwarders and shows their type-specific fields', async () => {
    mockForwarders = [
      forwarder('datadog', { configured: true, enabled: true }),
      forwarder('pagerduty', { configured: true }),
      forwarder('rollbar', { configured: true }),
      forwarder('opsgenie', { configured: true }),
      forwarder('loki', { configured: true }),
    ]
    const wrapper = mountView()
    await flushPromises()

    const datadog = cardFor(wrapper, 'Datadog')
    expect(datadog.find('#settingserrorforwardersview-field-10').exists()).toBe(true)
    const pagerduty = cardFor(wrapper, 'Pagerduty')
    expect(pagerduty.find('#settingserrorforwardersview-field-8').exists()).toBe(true)
    const rollbar = cardFor(wrapper, 'Rollbar')
    expect(rollbar.find('#settingserrorforwardersview-field-7').exists()).toBe(true)
    const opsgenie = cardFor(wrapper, 'Opsgenie')
    expect(opsgenie.find('#settingserrorforwardersview-field-5').exists()).toBe(true)
    const loki = cardFor(wrapper, 'Loki')
    expect(loki.find('#settingserrorforwardersview-field-3').exists()).toBe(true)
    // rollbar has the extra environment field; loki has tenant + labels + hint
    expect(rollbar.find('#settingserrorforwardersview-field-6').exists()).toBe(true)
    expect(loki.find('#settingserrorforwardersview-field-1').exists()).toBe(true)
    expect(loki.text()).toContain('Comma-separated key=value pairs')
    wrapper.unmount()
  })

  it('unconfigured forwarders start collapsed and expand via the loader-level expand state', async () => {
    // NOTE: expanding an unconfigured forwarder through the toggle is broken
    // (see the BUG test below); assert the collapsed default here.
    mockForwarders = [forwarder('loki')]
    const wrapper = mountView()
    await flushPromises()

    expect(cardFor(wrapper, 'Loki').find('#settingserrorforwardersview-field-3').exists()).toBe(false)
    wrapper.unmount()
  })

  it('BUG: toggling a forwarder off then saving persists the stale enabled state (readonly vue-query data)', async () => {
    // Production bug characterisation. toggleForwarder() does
    // `fwd.enabled = !fwd.enabled` on an item that comes from @tanstack/vue-query's
    // deep-readonly query state (useBaseQuery wraps state in readonly(state)).
    // Vue silently drops the write, so:
    //   1. the switch never flips visually,
    //   2. the config panel never expands for an unconfigured forwarder
    //      (expansion is gated on `fwd.enabled` becoming true), and
    //   3. Save persists the STALE original enabled value.
    mockForwarders = [forwarder('loki', { configured: true, enabled: true })]
    const wrapper = mountView()
    await flushPromises()

    // The user's intent: disable this forwarder.
    await wrapper.find('button[aria-label="Toggle Loki"]').trigger('click')
    await nextTick()

    const saveBtn = findButton(cardFor(wrapper, 'Loki'), 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    // The PUT went out with the STALE `enabled: true`, not the intended false.
    const put = vi.mocked(api.PUT).mock.calls[0]
    expect((put[1] as any).body.enabled).toBe(true)
    wrapper.unmount()
  })

  it('BUG: the Sentry DSN placeholder translation fails to compile ("@" is linked-message syntax)', async () => {
    // Production bug characterisation. en-US.js ships
    // `dsn_placeholder: "https://key@sentry.io/123"`; vue-i18n's message
    // compiler treats `@` as linked-message syntax and throws
    // SyntaxError: Message compilation error: Invalid linked format.
    // Any expanded Sentry panel evaluates this placeholder during render, so
    // the component render aborts and the panel never renders its fields.
    const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })
    expect(() => i18n.global.t('views.SettingsErrorForwardersView.dsn_placeholder')).toThrow(SyntaxError)
    // sanity: non-@ placeholder messages compile fine
    expect(i18n.global.t('views.SettingsErrorForwardersView.push_url_placeholder')).toBe(
      'https://loki.example.com/loki/api/v1/push',
    )
  })

  it('saves only the non-empty config fields for a forwarder', async () => {
    mockForwarders = [forwarder('loki', { configured: true, enabled: true })]
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('#settingserrorforwardersview-field-3').setValue('https://loki.example.com/push')
    await wrapper.find('#settingserrorforwardersview-field-2').setValue('my-tenant')
    // labels (field-1) left empty → stripped from the payload
    const saveBtn = findButton(cardFor(wrapper, 'Loki'), 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const put = vi.mocked(api.PUT).mock.calls[0]
    expect(put[0]).toBe('/api/v1/errors/forwarders/{forwarder_type}')
    expect((put[1] as any).params.path.forwarder_type).toBe('loki')
    expect((put[1] as any).body).toEqual({
      enabled: true,
      config_json: { push_url: 'https://loki.example.com/push', tenant_id: 'my-tenant' },
    })
    wrapper.unmount()
  })

  it('shows the saved confirmation after a successful save', async () => {
    vi.useFakeTimers()
    mockForwarders = [forwarder('loki', { configured: true, enabled: true })]
    const wrapper = mountView()
    await flushPromises()

    const saveBtn = findButton(cardFor(wrapper, 'Loki'), 'Save')
    await saveBtn!.trigger('click')
    await vi.advanceTimersByTimeAsync(0)
    await nextTick()
    expect(wrapper.text()).toContain('Configuration saved.')

    // The message auto-clears after 3s.
    await vi.advanceTimersByTimeAsync(3000)
    await nextTick()
    expect(wrapper.text()).not.toContain('Configuration saved.')
    wrapper.unmount()
  })

  it('shows an inline save error when the PUT fails', async () => {
    mockForwarders = [forwarder('opsgenie', { configured: true, enabled: true })]
    mockPutError = { detail: 'invalid_api_key' }
    const wrapper = mountView()
    await flushPromises()

    const saveBtn = findButton(cardFor(wrapper, 'Opsgenie'), 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Save failed:')
    expect(wrapper.text()).toContain('invalid_api_key')
    wrapper.unmount()
  })

  it('tests the connection and renders the success result, auto-clearing after 10s', async () => {
    vi.useFakeTimers()
    mockForwarders = [forwarder('loki', { configured: true, enabled: true })]
    mockTestResult = { ok: true, message: 'Push URL accepted' }
    const wrapper = mountView()
    await flushPromises()

    const testBtn = findButton(cardFor(wrapper, 'Loki'), 'Test Connection')
    expect(testBtn).toBeTruthy()
    await testBtn!.trigger('click')
    await vi.advanceTimersByTimeAsync(0)
    await nextTick()

    expect(vi.mocked(api.POST).mock.calls[0][0]).toBe('/api/v1/errors/forwarders/{forwarder_type}/test')
    expect(wrapper.text()).toContain('Connection successful')
    expect(wrapper.text()).toContain('Push URL accepted')
    // result auto-clears after 10s
    await vi.advanceTimersByTimeAsync(10000)
    await nextTick()
    expect(wrapper.text()).not.toContain('Push URL accepted')
    wrapper.unmount()
  })

  it('renders a failed test result from the response body', async () => {
    mockForwarders = [forwarder('pagerduty', { configured: true, enabled: true })]
    mockTestResult = { ok: false, message: '401 invalid routing key' }
    const wrapper = mountView()
    await flushPromises()

    const testBtn = findButton(cardFor(wrapper, 'Pagerduty'), 'Test Connection')
    await testBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Connection failed')
    expect(wrapper.text()).toContain('401 invalid routing key')
    wrapper.unmount()
  })

  it('renders a thrown-test error as a failed result', async () => {
    mockForwarders = [forwarder('pagerduty', { configured: true, enabled: true })]
    mockTestThrows = new Error('network down')
    const wrapper = mountView()
    await flushPromises()

    const testBtn = findButton(cardFor(wrapper, 'Pagerduty'), 'Test Connection')
    await testBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Connection failed')
    expect(wrapper.text()).toContain('network down')
    wrapper.unmount()
  })

  it('renders the test error envelope when the test endpoint returns an error', async () => {
    mockForwarders = [forwarder('opsgenie', { configured: true, enabled: true })]
    mockTestError = { detail: 'test_not_implemented' }
    const wrapper = mountView()
    await flushPromises()

    const testBtn = findButton(cardFor(wrapper, 'Opsgenie'), 'Test Connection')
    await testBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Connection failed')
    expect(wrapper.text()).toContain('test_not_implemented')
    wrapper.unmount()
  })
})
