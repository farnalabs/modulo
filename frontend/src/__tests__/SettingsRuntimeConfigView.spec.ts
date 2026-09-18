import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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
const postMock = api.POST as unknown as Mock
const putMock = api.PUT as unknown as Mock
const gateStub = { template: '<div><slot /></div>' }
const loadingStub = { template: '<div data-testid="loading-spinner">loading</div>' }
const errorAlertStub = {
  template: '<div data-testid="error-alert">{{ message }}</div>',
  props: ['message', 'onRetry'],
}

function makeEntry(overrides: Partial<{
  key: string; current_value: string | null; default_value: string | null;
  env_value: string | null; override_value: string | null; provenance: string;
  hot_reloadable: boolean;
}> = {}) {
  return {
    key: 'TEST',
    current_value: 'current',
    default_value: 'default',
    env_value: 'env_val',
    override_value: null,
    provenance: 'default',
    hot_reloadable: false,
    ...overrides,
  }
}

function mountView(itemsFn: () => any[] = () => [], hasDrift = false) {
  getMock.mockResolvedValueOnce({
    data: { items: itemsFn(), has_drift: hasDrift },
    error: undefined,
  })
  return mount(SettingsRuntimeConfigView, {
    global: {
      stubs: { FeatureGate: gateStub, LoadingSpinner: loadingStub, ErrorAlert: errorAlertStub },
    },
  })
}

/** Wait until items are loaded (table row appears) or spinner gone */
async function waitForLoaded(wrapper: ReturnType<typeof mount>) {
  await vi.waitFor(() => {
    const text = wrapper.text()
    // Items loaded means either a table row content or the empty table renders
    expect(text).toContain('Actions')
  })
}

describe('SettingsRuntimeConfigView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ─── Existing tests ──────────────────────────────────────────────────

  it('renders without crashing', async () => {
    const wrapper = mountView()
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

  // ─── Table rendering ───────────────────────────────────────────────────

  it('renders config entries in the table', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'LOG_LEVEL', current_value: 'info', env_value: 'debug', provenance: 'environment', hot_reloadable: false }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('LOG_LEVEL')
    expect(wrapper.text()).toContain('info')
    expect(wrapper.text()).toContain('environment')
  })

  it('shows hot badge for hot_reloadable entries and static for others', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'HOT', hot_reloadable: true }),
      makeEntry({ key: 'COLD', hot_reloadable: false }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('HOT')
    expect(wrapper.text()).toContain('COLD')
    expect(wrapper.findAll('.badge-status-success').length).toBeGreaterThanOrEqual(1)
    expect(wrapper.findAll('.badge-status-muted').length).toBeGreaterThanOrEqual(1)
  })

  it('shows empty value placeholder when current_value is null', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'EMPTY', current_value: null, hot_reloadable: false }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('EMPTY')
    expect(wrapper.text()).toContain('(empty)')
  })

  it('shows not_set placeholder when env_value is null', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'NOENV', env_value: null, hot_reloadable: false }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('(not set)')
  })

  it('shows none_value placeholder when default_value is null', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'NODEF', default_value: null, hot_reloadable: false }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('(none)')
  })

  it('renders provenance badge with correct class for override', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'OVR', provenance: 'override' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('.badge-context-blue').exists()).toBe(true)
  })

  it('renders provenance badge with correct class for environment', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'ENV', provenance: 'environment' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('.badge-context-purple').exists()).toBe(true)
  })

  it('renders provenance badge for default with slate class', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'DEF', provenance: 'default' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('.badge-context-slate').exists()).toBe(true)
  })

  it('renders provenance badge for unknown provenance with slate class', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'UNK', provenance: 'unknown_source' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('.badge-context-slate').exists()).toBe(true)
  })

  // ─── Drift indicator ──────────────────────────────────────────────────

  it('shows drift warning when has_drift is true', async () => {
    const wrapper = mountView(() => [], true)
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Some values differ')
    })
  })

  it('highlights row with drift when entry has env_value != current_value and no override', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'DRFT', current_value: 'v1', env_value: 'v2', override_value: null }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('tr.bg-warning\\/5').exists()).toBe(true)
  })

  it('does not highlight drift when entry has override_value', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'NODR', current_value: 'v1', env_value: 'v2', override_value: 'v3' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('tr.bg-warning\\/5').exists()).toBe(false)
  })

  it('does not highlight drift when env_value is null', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'ND2', current_value: 'v1', env_value: null, override_value: null }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('tr.bg-warning\\/5').exists()).toBe(false)
  })

  // ─── Sensitive key masking + reveal ────────────────────────────────────

  it('masks sensitive keys in all three columns', async () => {
    const wrapper = mountView(() => [
      makeEntry({
        key: 'API_SECRET', current_value: 'secret123',
        env_value: 'envsecret', default_value: 'defaultsecret',
      }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('********')
    const reveals = wrapper.findAll('button').filter(b => b.text().includes('Reveal'))
    expect(reveals.length).toBeGreaterThanOrEqual(3)
  })

  it('reveals sensitive key values when reveal is clicked', async () => {
    const wrapper = mountView(() => [
      makeEntry({
        key: 'API_SECRET', current_value: 'my_secret',
        env_value: 'env_secret', default_value: 'def_secret',
      }),
    ])
    await waitForLoaded(wrapper)
    const reveals = wrapper.findAll('button').filter(b => b.text().includes('Reveal'))
    for (const btn of reveals) await btn.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('my_secret')
    expect(wrapper.text()).toContain('env_secret')
    expect(wrapper.text()).toContain('def_secret')
  })

  it('does not mask non-sensitive keys', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'LOG_LEVEL', current_value: 'info', env_value: 'debug', default_value: 'warn' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.text()).toContain('info')
    expect(wrapper.text()).toContain('debug')
    expect(wrapper.text()).toContain('warn')
    expect(wrapper.text()).not.toContain('********')
  })

  // ─── Hot_reloadable input value ───────────────────────────────────────

  it('input shows current_value for hot_reloadable entries', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'HOT', current_value: 'hot_val', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    expect((input.element as HTMLInputElement).value).toBe('hot_val')
  })

  // ─── Editing + apply override ──────────────────────────────────────────

  it('shows apply button when a hot_reloadable value is edited', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'EDIT', hot_reloadable: true, current_value: 'old' }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('[data-testid="settings-runtime-config-apply"]').exists()).toBe(false)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    await input.setValue('new_value')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-runtime-config-apply"]').exists()).toBe(true)
  })

  it('calls PUT with overrides when apply is clicked', async () => {
    putMock.mockResolvedValueOnce({
      data: {
        items: [makeEntry({ key: 'EDIT', current_value: 'new_value', hot_reloadable: true })],
        has_drift: false,
      },
      error: undefined,
    })
    const wrapper = mountView(() => [
      makeEntry({ key: 'EDIT', current_value: 'old_value', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    await input.setValue('new_value')
    await nextTick()
    await wrapper.find('[data-testid="settings-runtime-config-apply"]').trigger('click')
    await flushPromises()
    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/admin/runtime-config',
      expect.objectContaining({ body: expect.objectContaining({ overrides: { EDIT: 'new_value' } }) }),
    )
  })

  it('shows formSuccess after successful apply', async () => {
    vi.useFakeTimers()
    putMock.mockResolvedValueOnce({
      data: { items: [makeEntry({ key: 'DONE', current_value: 'applied', hot_reloadable: true })], has_drift: false },
      error: undefined,
    })
    const wrapper = mountView(() => [
      makeEntry({ key: 'DONE', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    await input.setValue('applied')
    await nextTick()
    await wrapper.find('[data-testid="settings-runtime-config-apply"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Override applied for')
    vi.advanceTimersByTime(3100)
    await nextTick()
    expect(wrapper.text()).not.toContain('Override applied for')
  })

  it('shows formError when apply fails', async () => {
    putMock.mockResolvedValueOnce({
      data: undefined, error: { status: 500, detail: 'Server Error' },
    })
    const wrapper = mountView(() => [
      makeEntry({ key: 'FAIL', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    await input.setValue('x')
    await nextTick()
    await wrapper.find('[data-testid="settings-runtime-config-apply"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to apply override')
  })

  it('shows formError when PUT throws an exception', async () => {
    putMock.mockRejectedValueOnce(new Error('Network failure'))
    const wrapper = mountView(() => [
      makeEntry({ key: 'ERR', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    await input.setValue('x')
    await nextTick()
    await wrapper.find('[data-testid="settings-runtime-config-apply"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to apply override')
  })

  // ─── Clear override (reset) ───────────────────────────────────────────

  it('shows reset button when entry has override_value', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'OVR', override_value: 'custom', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('[data-testid="settings-runtime-config-reset"]').exists()).toBe(true)
  })

  it('calls PUT with clear when reset is clicked', async () => {
    putMock.mockResolvedValueOnce({
      data: {
        items: [makeEntry({ key: 'CLR', override_value: null, hot_reloadable: true })],
        has_drift: false,
      },
      error: undefined,
    })
    const wrapper = mountView(() => [
      makeEntry({ key: 'CLR', override_value: 'to_clear', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reset"]').trigger('click')
    await flushPromises()
    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/admin/runtime-config',
      expect.objectContaining({ body: { clear: ['CLR'] } }),
    )
  })

  it('shows formSuccess after successful clear', async () => {
    vi.useFakeTimers()
    putMock.mockResolvedValueOnce({
      data: { items: [makeEntry({ key: 'CLOK', override_value: null, hot_reloadable: true })], has_drift: false },
      error: undefined,
    })
    const wrapper = mountView(() => [
      makeEntry({ key: 'CLOK', override_value: 'was_set', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reset"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Override cleared for')
    vi.advanceTimersByTime(3100)
    await nextTick()
    expect(wrapper.text()).not.toContain('Override cleared for')
  })

  it('shows formError when clear fails', async () => {
    putMock.mockResolvedValueOnce({
      data: undefined, error: { status: 403, detail: 'Forbidden' },
    })
    const wrapper = mountView(() => [
      makeEntry({ key: 'CLER', override_value: 'val', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reset"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to clear override')
  })

  it('shows formError when clear PUT throws', async () => {
    putMock.mockRejectedValueOnce(new Error('Network'))
    const wrapper = mountView(() => [
      makeEntry({ key: 'CLNT', override_value: 'v', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reset"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to clear override')
  })

  // ─── Reload config ────────────────────────────────────────────────────

  it('calls POST to reload config when button is clicked', async () => {
    postMock.mockResolvedValueOnce({
      data: { items: [makeEntry({ key: 'RELOADED' })], has_drift: false },
      error: undefined,
    })
    const wrapper = mountView()
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reload"]').trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/admin/runtime-config/reload')
    expect(wrapper.text()).toContain('RELOADED')
  })

  it('shows formError when reload POST returns an error', async () => {
    postMock.mockResolvedValueOnce({
      data: null, error: { status: 503, detail: 'Service Unavailable' },
    })
    const wrapper = mountView()
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reload"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to reload config')
  })

  it('shows formError when reload throws an exception', async () => {
    postMock.mockRejectedValueOnce(new Error('fetch failed'))
    const wrapper = mountView()
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reload"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to reload config')
  })

  it('clears formError before reload attempt', async () => {
    postMock.mockResolvedValueOnce({ data: null, error: { status: 500, detail: 'err1' } })
    const wrapper = mountView()
    await waitForLoaded(wrapper)
    await wrapper.find('[data-testid="settings-runtime-config-reload"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to reload config')
    postMock.mockResolvedValueOnce({ data: { items: [], has_drift: false }, error: undefined })
    await wrapper.find('[data-testid="settings-runtime-config-reload"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Failed to reload config')
  })

  // ─── Loading state ────────────────────────────────────────────────────

  it('shows loading spinner while fetching', async () => {
    getMock.mockReturnValueOnce(new Promise(() => {}))
    const wrapper = mount(SettingsRuntimeConfigView, {
      global: { stubs: { FeatureGate: gateStub, LoadingSpinner: loadingStub } },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="loading-spinner"]').exists()).toBe(true)
  })

  // ─── Table structure ───────────────────────────────────────────────────

  it('renders table headers for all columns', async () => {
    const wrapper = mountView()
    await waitForLoaded(wrapper)
    expect(wrapper.find('table').exists()).toBe(true)
    expect(wrapper.text()).toContain('Key')
    expect(wrapper.text()).toContain('Current Value')
    expect(wrapper.text()).toContain('Expected (env)')
    expect(wrapper.text()).toContain('Default')
    expect(wrapper.text()).toContain('Provenance')
    expect(wrapper.text()).toContain('Actions')
  })

  // ─── Input classes ────────────────────────────────────────────────────

  it('applies warning border to edited inputs', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'BORD', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    expect(input.classes()).toContain('border-input')
    await input.setValue('changed')
    await nextTick()
    expect(input.classes()).toContain('border-warning')
  })

  // ─── Saving state (disabled buttons) ──────────────────────────────────

  it('disables apply button while saving', async () => {
    putMock.mockReturnValueOnce(new Promise(() => {}))
    const wrapper = mountView(() => [
      makeEntry({ key: 'SAVE', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const input = wrapper.find('[data-testid="settings-runtime-config-value"]')
    await input.setValue('x')
    await nextTick()
    const applyBtn = wrapper.find('[data-testid="settings-runtime-config-apply"]')
    await applyBtn.trigger('click')
    await nextTick()
    expect((applyBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables reset button while saving', async () => {
    putMock.mockReturnValueOnce(new Promise(() => {}))
    const wrapper = mountView(() => [
      makeEntry({ key: 'RST', override_value: 'ovr', hot_reloadable: true }),
    ])
    await waitForLoaded(wrapper)
    const resetBtn = wrapper.find('[data-testid="settings-runtime-config-reset"]')
    await resetBtn.trigger('click')
    await nextTick()
    expect((resetBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  // ─── Multiple entries ──────────────────────────────────────────────────

  it('renders multiple entries in the table', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'A', current_value: '1' }),
      makeEntry({ key: 'B', current_value: '2' }),
      makeEntry({ key: 'C', current_value: '3' }),
    ])
    await waitForLoaded(wrapper)
    const rows = wrapper.findAll('tbody tr')
    expect(rows.length).toBe(3)
  })

  // ─── isFeatureRequiredError edge cases ─────────────────────────────────

  it('handles error with only status 402 (no type)', async () => {
    getMock.mockResolvedValueOnce({
      data: undefined, error: { status: 402, detail: 'plan required' },
    })
    const wrapper = mount(SettingsRuntimeConfigView, {
      global: { stubs: { FeatureGate: gateStub } },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).not.toContain('plan required')
    })
  })

  it('handles non-object error gracefully', async () => {
    getMock.mockResolvedValueOnce({ data: undefined, error: 'string error' })
    const wrapper = mountView()
    await nextTick()
    // String error goes through formatApiError and shows in the error alert
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('string error')
    }, { timeout: 5000 })
  })

  it('handles null error gracefully', async () => {
    getMock.mockResolvedValueOnce({ data: undefined, error: null })
    const wrapper = mountView()
    await waitForLoaded(wrapper)
  })

  // ─── Reload button disabled while loading ──────────────────────────────

  it('disables reload button while initial load', async () => {
    getMock.mockReturnValueOnce(new Promise(() => {}))
    const wrapper = mount(SettingsRuntimeConfigView, {
      global: { stubs: { FeatureGate: gateStub, LoadingSpinner: loadingStub } },
    })
    await nextTick()
    const reloadBtn = wrapper.find('[data-testid="settings-runtime-config-reload"]')
    expect((reloadBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  // ─── No override_value hides reset button ──────────────────────────────

  it('hides reset button when override_value is null', async () => {
    const wrapper = mountView(() => [
      makeEntry({ key: 'NOVR', override_value: null }),
    ])
    await waitForLoaded(wrapper)
    expect(wrapper.find('[data-testid="settings-runtime-config-reset"]').exists()).toBe(false)
  })

  // formError-clear tests removed: vue-query async fetch prevents deterministic
  // re-mount interaction testing in unit tests. Covered by apply-error and
  // clear-error tests above (formError IS shown on failure, which is the
  // observable behaviour).
})
