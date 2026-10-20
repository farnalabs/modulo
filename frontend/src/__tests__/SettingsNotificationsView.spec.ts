import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    GET: vi.fn(),
    PUT: vi.fn(),
  },
}))

vi.mock('../lib/api/client', () => ({
  api: mockApi,
}))

import SettingsNotificationsView from '../views/SettingsNotificationsView.vue'

const PREFERENCES_PATH = '/api/v1/notifications/in-app/preferences'

interface PreferencesPayload {
  dashboard_level?: string
  notification_opt_outs: Record<string, boolean>
}

function okGet(payload: PreferencesPayload) {
  mockApi.GET.mockResolvedValue({ data: payload, error: undefined })
}

function okPut() {
  mockApi.PUT.mockResolvedValue({ data: {}, error: undefined })
}

async function mountView() {
  const wrapper = mount(SettingsNotificationsView)
  await flushPromises()
  await nextTick()
  return wrapper
}

function switches(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('[role="switch"]')
}

function switchByCategory(wrapper: ReturnType<typeof mount>, category: string) {
  return wrapper.find(`[data-category="${category}"]`)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

describe('SettingsNotificationsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    okPut()
  })

  it('renders one toggle per category key returned by GET', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'hitl.awaiting': false, 'run.failed': false },
    })
    const wrapper = await mountView()

    expect(switches(wrapper)).toHaveLength(2)
    expect(switchByCategory(wrapper, 'hitl.awaiting').exists()).toBe(true)
    expect(switchByCategory(wrapper, 'run.failed').exists()).toBe(true)
  })

  it('restores the saved opt-out state on reload (true means disabled)', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': true, 'hitl.awaiting': false },
    })
    const first = await mountView()

    expect(switchByCategory(first, 'run.failed').attributes('aria-checked')).toBe('false')
    expect(switchByCategory(first, 'hitl.awaiting').attributes('aria-checked')).toBe('true')
    first.unmount()

    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': false, 'hitl.awaiting': true },
    })
    const second = await mountView()

    expect(switchByCategory(second, 'run.failed').attributes('aria-checked')).toBe('true')
    expect(switchByCategory(second, 'hitl.awaiting').attributes('aria-checked')).toBe('false')
  })

  it('sends a single-key delta with the snake_case category when disabling one', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': false, 'hitl.awaiting': false },
    })
    const wrapper = await mountView()

    await switchByCategory(wrapper, 'hitl.awaiting').trigger('click')
    await flushPromises()

    expect(mockApi.PUT).toHaveBeenCalledTimes(1)
    const [path, options] = mockApi.PUT.mock.calls[0]
    expect(path).toBe(PREFERENCES_PATH)
    expect(options.body).toEqual({ notification_opt_outs: { 'hitl.awaiting': true } })
    expect(switchByCategory(wrapper, 'hitl.awaiting').attributes('aria-checked')).toBe('false')
  })

  it('sends opted-out=false when re-enabling a category', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': true },
    })
    const wrapper = await mountView()

    await switchByCategory(wrapper, 'run.failed').trigger('click')
    await flushPromises()

    expect(mockApi.PUT.mock.calls[0][1].body).toEqual({
      notification_opt_outs: { 'run.failed': false },
    })
  })

  it('shows a loading state (and no toggles) while the initial GET is pending', async () => {
    const pending = deferred<{ data: PreferencesPayload, error: undefined }>()
    mockApi.GET.mockReturnValue(pending.promise)

    const wrapper = mount(SettingsNotificationsView)
    await nextTick()

    expect(wrapper.find('[data-testid="settings-notifications-loading"]').exists()).toBe(true)
    expect(switches(wrapper)).toHaveLength(0)

    pending.resolve({
      data: { notification_opt_outs: { 'run.failed': false } },
      error: undefined,
    })
    await flushPromises()
    expect(switches(wrapper)).toHaveLength(1)
  })

  it('disables every toggle while a save is in flight', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': false, 'hitl.awaiting': false },
    })
    const pendingPut = deferred<{ data: Record<string, never>, error: undefined }>()
    mockApi.PUT.mockReturnValue(pendingPut.promise)
    const wrapper = await mountView()

    await switchByCategory(wrapper, 'run.failed').trigger('click')
    await nextTick()

    for (const toggle of switches(wrapper)) {
      expect(toggle.attributes('disabled')).toBeDefined()
    }

    pendingPut.resolve({ data: {}, error: undefined })
    await flushPromises()
    expect(switchByCategory(wrapper, 'run.failed').attributes('disabled')).toBeUndefined()
  })

  it('surfaces a GET failure inline and retries the load', async () => {
    mockApi.GET.mockResolvedValue({ data: undefined, error: { detail: 'backend down' } })
    const wrapper = await mountView()

    const errorRegion = wrapper.find('[data-testid="settings-notifications-error"]')
    expect(errorRegion.exists()).toBe(true)
    expect(errorRegion.attributes('aria-live')).toBe('assertive')
    expect(errorRegion.text()).toContain('backend down')
    expect(errorRegion.find('[data-testid="error-alert-retry"]').exists()).toBe(true)

    okGet({ notification_opt_outs: { 'run.failed': false } })
    await errorRegion.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(mockApi.GET).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="settings-notifications-error"]').exists()).toBe(false)
    expect(switches(wrapper)).toHaveLength(1)
  })

  it('surfaces a PUT failure inline, keeps the previous state, and retries the same delta', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': false },
    })
    mockApi.PUT.mockResolvedValue({ data: undefined, error: { detail: 'save rejected' } })
    const wrapper = await mountView()

    await switchByCategory(wrapper, 'run.failed').trigger('click')
    await flushPromises()

    const saveError = wrapper.find('[data-testid="settings-notifications-save-error"]')
    expect(saveError.exists()).toBe(true)
    expect(saveError.attributes('aria-live')).toBe('assertive')
    expect(saveError.text()).toContain('save rejected')
    // The optimistic state was NOT applied: the category is still enabled.
    expect(switchByCategory(wrapper, 'run.failed').attributes('aria-checked')).toBe('true')

    okPut()
    await saveError.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(mockApi.PUT).toHaveBeenCalledTimes(2)
    expect(mockApi.PUT.mock.calls[1][1].body).toEqual({
      notification_opt_outs: { 'run.failed': true },
    })
    expect(wrapper.find('[data-testid="settings-notifications-save-error"]').exists()).toBe(false)
    expect(switchByCategory(wrapper, 'run.failed').attributes('aria-checked')).toBe('false')
  })

  it('sends a dashboard_level-only delta from the level selector', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': false },
    })
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-notifications-level"]').setValue('error')
    await flushPromises()

    expect(mockApi.PUT).toHaveBeenCalledTimes(1)
    expect(mockApi.PUT.mock.calls[0][1].body).toEqual({ dashboard_level: 'error' })
  })
})
