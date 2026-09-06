import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { getMock, putMock, loadBackendsMock, getErrorTrackerMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  putMock: vi.fn(),
  loadBackendsMock: vi.fn(),
  getErrorTrackerMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: getMock,
    POST: vi.fn(),
    PUT: putMock,
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../monitor', () => ({
  loadBackends: loadBackendsMock,
}))

vi.mock('../lib/error-tracking', () => ({
  getErrorTracker: getErrorTrackerMock,
}))

import SettingsMonitorConfigView from '../views/SettingsMonitorConfigView.vue'

async function flush() {
  await flushPromises()
  await nextTick()
}

function findButtonByText(wrapper: ReturnType<typeof mount>, text: string) {
  return wrapper.findAll('button').find((b) => b.text().trim() === text)
}

function toggles(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('input[type="checkbox"]')
}

describe('SettingsMonitorConfigView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    getMock.mockResolvedValue({ data: { backends: ['builtin'] }, error: undefined })
    putMock.mockResolvedValue({ data: { backends: ['builtin'] }, error: undefined })
    loadBackendsMock.mockResolvedValue([])
    getErrorTrackerMock.mockReturnValue(null)
  })

  it('renders the explainer and four backend sections after load', async () => {
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    expect(wrapper.text()).toContain('Browser Monitoring')
    expect(wrapper.text()).toContain('Built-in (DB)')
    expect(wrapper.text()).toContain('Sentry')
    expect(wrapper.text()).toContain('Datadog RUM')
    expect(wrapper.text()).toContain('Grafana Faro')
    expect(toggles(wrapper)).toHaveLength(4)
  })

  it('fromApiPayload: enabled backends hydrate toggles and field values', async () => {
    getMock.mockResolvedValue({
      data: { backends: ['builtin', 'sentry'], sentry: { dsn: 'https://sentry.example/1' } },
      error: undefined,
    })
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    const [builtin, sentry, datadog, faro] = toggles(wrapper)
    expect((builtin.element as HTMLInputElement).checked).toBe(true)
    expect((sentry.element as HTMLInputElement).checked).toBe(true)
    expect((datadog.element as HTMLInputElement).checked).toBe(false)
    expect((faro.element as HTMLInputElement).checked).toBe(false)

    // Sentry fields revealed because the backend is enabled; the stored DSN is prefilled.
    const dsn = wrapper.find('input[type="password"]')
    expect(dsn.exists()).toBe(true)
    expect((dsn.element as HTMLInputElement).value).toBe('https://sentry.example/1')
  })

  it('an empty backends payload falls back to builtin', async () => {
    getMock.mockResolvedValue({ data: { backends: [] }, error: undefined })
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    expect((toggles(wrapper)[0].element as HTMLInputElement).checked).toBe(true)
  })

  it('a load failure shows the error with a working Retry button', async () => {
    getMock.mockRejectedValue(new Error('config unreachable'))
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    const errorBox = wrapper.find('.text-destructive')
    expect(errorBox.exists()).toBe(true)
    expect(wrapper.text()).toContain('config unreachable')
    expect(wrapper.text()).toContain('Retry')

    getMock.mockResolvedValue({ data: { backends: ['builtin'] }, error: undefined })
    await findButtonByText(wrapper, 'Retry')!.trigger('click')
    await flush()

    expect(getMock).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.animate-spin').exists()).toBe(false)
  })

  it('toggling a backend reveals its fields; secrets are masked until revealed', async () => {
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    // Sentry disabled initially: no DSN input.
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)

    const [, sentry] = toggles(wrapper)
    await sentry.setValue(true)
    await nextTick()

    const dsn = wrapper.find('input[type="password"]')
    expect(dsn.exists()).toBe(true)
    expect(dsn.attributes('type')).toBe('password')

    await wrapper.findAll('button').find((b) => b.text() === 'Show')!.trigger('click')
    await nextTick()
    expect(wrapper.find('input[type="text"]').exists()).toBe(true)

    await sentry.setValue(false)
    await nextTick()
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
  })

  it('save is disabled until something is dirty', async () => {
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    expect(findButtonByText(wrapper, 'Save')!.attributes('disabled')).toBeDefined()

    const [, sentry] = toggles(wrapper)
    await sentry.setValue(true)
    await nextTick()
    expect(findButtonByText(wrapper, 'Save')!.attributes('disabled')).toBeUndefined()
  })

  it('save: PUT carries the active backends and per-backend fields, then reloads runtimes and shows success', async () => {
    putMock.mockResolvedValue({ data: { backends: ['builtin', 'datadog_rum'], datadog_rum: { clientToken: 'pub42', site: 'datadoghq.com' } }, error: undefined })
    const tracker = { reloadBackends: vi.fn() }
    getErrorTrackerMock.mockReturnValue(tracker)

    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    const [, , datadog] = toggles(wrapper)
    await datadog.setValue(true)
    await nextTick()

    // Datadog fields: clientToken (secret) and site (plain, prefilled).
    const clientToken = wrapper.find('input[type="password"]')
    await clientToken.setValue('pub42')

    await findButtonByText(wrapper, 'Save')!.trigger('click')
    await flush()

    expect(putMock).toHaveBeenCalledWith('/api/v1/admin/monitor-config', {
      body: {
        backends: ['builtin', 'datadog_rum'],
        // The stored-config load cleared the site default (fromApiPayload only
        // hydrates fields present in the stored payload), so only clientToken.
        datadog_rum: { clientToken: 'pub42' },
      },
    })
    expect(loadBackendsMock).toHaveBeenCalledTimes(1)
    const cfg = loadBackendsMock.mock.calls[0][0]
    expect(cfg.monitorBackends).toEqual(['builtin', 'datadog_rum'])
    expect(cfg.datadogRum).toEqual({ clientToken: 'pub42' })
    expect(tracker.reloadBackends).toHaveBeenCalledWith([])
    expect(wrapper.text()).toContain('Configuration saved')
    // Dirty flag reset from the saved payload: save disabled again.
    expect(findButtonByText(wrapper, 'Save')!.attributes('disabled')).toBeDefined()
  })

  it('save: an API error payload flashes the failure without enabling the tracker reload', async () => {
    putMock.mockResolvedValue({ data: null, error: { detail: 'validation exploded' } })
    const tracker = { reloadBackends: vi.fn() }
    getErrorTrackerMock.mockReturnValue(tracker)

    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    const [, sentry] = toggles(wrapper)
    await sentry.setValue(true)
    await findButtonByText(wrapper, 'Save')!.trigger('click')
    await flush()

    expect(wrapper.text()).toContain('Failed to save')
    expect(wrapper.text()).toContain('validation exploded')
    expect(loadBackendsMock).not.toHaveBeenCalled()
    expect(tracker.reloadBackends).not.toHaveBeenCalled()
  })

  it('save: a thrown PUT error flashes the formatted failure', async () => {
    putMock.mockRejectedValue(new Error('socket hang up'))
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()

    const [, sentry] = toggles(wrapper)
    await sentry.setValue(true)
    await findButtonByText(wrapper, 'Save')!.trigger('click')
    await flush()

    expect(wrapper.text()).toContain('Failed to save')
    expect(wrapper.text()).toContain('socket hang up')
  })

  it('reset re-fetches the stored config (only enabled once the form is dirty)', async () => {
    const wrapper = mount(SettingsMonitorConfigView)
    await flush()
    expect(getMock).toHaveBeenCalledTimes(1)

    // Reset is disabled while the form is clean.
    const resetBtn = findButtonByText(wrapper, 'Reset')!
    expect(resetBtn.attributes('disabled')).toBeDefined()

    const [, sentry] = toggles(wrapper)
    await sentry.setValue(true)
    await nextTick()
    expect(resetBtn.attributes('disabled')).toBeUndefined()

    await resetBtn.trigger('click')
    await flush()

    expect(getMock).toHaveBeenCalledTimes(2)
  })
})
