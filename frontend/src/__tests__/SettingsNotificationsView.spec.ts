import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { readFileSync } from 'node:fs'
import { resolve as resolvePath } from 'node:path'

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
import enUS from '../locales/en-US.js'
import { getVisibleNavGroups } from '../config/navigation'
import type { NavVisibilityContext } from '../config/navigation'

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

function levelSelect(wrapper: ReturnType<typeof mount>) {
  return wrapper.find('[data-testid="settings-notifications-level"]')
}

function selectedLevel(wrapper: ReturnType<typeof mount>): string {
  return (levelSelect(wrapper).element as HTMLSelectElement).value
}

// The backend is the single source of truth for the category set
// (``_EVENT_CONFIG`` in event_mapper.py, surfaced via
// ``notification_categories()``). Deriving it from that file - rather than
// restating a list here - is what keeps this test honest when a category is
// added: a new backend category fails this assertion until it is labelled.
const EVENT_MAPPER_PATH = resolvePath(
  __dirname,
  '../../../backend/src/modulo/core/notifier/event_mapper.py',
)

function backendCategories(): string[] {
  const source = readFileSync(EVENT_MAPPER_PATH, 'utf8')
  const found = new Set<string>()
  for (const match of source.matchAll(/"category":\s*"([^"]+)"/g)) found.add(match[1])
  return [...found].sort()
}

// The locale bundle is a plain JS module, so narrow it structurally rather
// than fighting the vue-i18n message type in this spec.
const messages = enUS as unknown as {
  views: { SettingsNotificationsView: { categories: Record<string, unknown> } }
}

function categoryLabels(): Record<string, unknown> {
  return messages.views.SettingsNotificationsView.categories
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

    // Non-vacuous: assert the toggle set exists before asserting on every row.
    expect(switches(wrapper)).toHaveLength(2)
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
    // (c) the saved value is what the control shows afterwards.
    expect(selectedLevel(wrapper)).toBe('error')
  })

  it('renders the stored dashboard_level from the GET payload', async () => {
    okGet({
      dashboard_level: 'error',
      notification_opt_outs: { 'run.failed': false },
    })
    const wrapper = await mountView()

    // (a) the select reflects the stored level, not the client default.
    expect(selectedLevel(wrapper)).toBe('error')
  })

  it('falls back to warning when GET omits or invalidates the dashboard_level', async () => {
    okGet({ notification_opt_outs: { 'run.failed': false } })
    const omitted = await mountView()
    expect(selectedLevel(omitted)).toBe('warning')
    omitted.unmount()

    okGet({ dashboard_level: 'nonsense', notification_opt_outs: { 'run.failed': false } })
    const invalid = await mountView()
    // (b) an unrecognised level never reaches the control.
    expect(selectedLevel(invalid)).toBe('warning')
  })

  it('marks the saving switch busy and names the in-flight state in text', async () => {
    okGet({
      dashboard_level: 'warning',
      notification_opt_outs: { 'run.failed': false, 'hitl.awaiting': false },
    })
    const pendingPut = deferred<{ data: Record<string, never>, error: undefined }>()
    mockApi.PUT.mockReturnValue(pendingPut.promise)
    const wrapper = await mountView()

    // The polite region is mounted BEFORE its content changes.
    const status = wrapper.find('[data-testid="settings-notifications-status"]')
    expect(status.exists()).toBe(true)
    expect(status.attributes('aria-live')).toBe('polite')
    expect(status.text()).toBe('')

    await switchByCategory(wrapper, 'run.failed').trigger('click')
    await nextTick()

    // Not colour-only: the row says "Saving..." and the switch is aria-busy.
    expect(switchByCategory(wrapper, 'run.failed').attributes('aria-busy')).toBe('true')
    expect(switchByCategory(wrapper, 'hitl.awaiting').attributes('aria-busy')).toBeUndefined()
    const savingRow = switchByCategory(wrapper, 'run.failed').element.closest('li') as HTMLElement | null
    expect(savingRow?.textContent).toContain('Saving...')
    expect(wrapper.find('[data-testid="settings-notifications-status"]').text()).toContain('Saving...')

    pendingPut.resolve({ data: {}, error: undefined })
    await flushPromises()

    expect(switchByCategory(wrapper, 'run.failed').attributes('aria-busy')).toBeUndefined()
    expect(wrapper.find('[data-testid="settings-notifications-status"]').text()).toBe('')
  })

  it('gives every backend notification category a curated label', () => {
    const categories = backendCategories()
    // Guard the parse: an empty result would make the assertion below vacuous.
    expect(categories.length).toBeGreaterThan(0)

    const labels = categoryLabels()
    const missing = categories.filter((category) => {
      const label = category
        .split('.')
        .reduce<unknown>((node, part) => (node as Record<string, unknown> | undefined)?.[part], labels)
      return typeof label !== 'string' || label.trim() === ''
    })
    expect(missing).toEqual([])

    // Spot-check the previously unlabelled category so this cannot pass by
    // returning an empty set.
    const provider = labels.provider as Record<string, unknown> | undefined
    expect(provider).toBeDefined()
    expect(typeof provider?.failure).toBe('string')
  })

  it('keeps the notification-preferences nav item visible to every role', () => {
    const roles = ['viewer', 'runner', 'operator', 'admin']
    const baseContext: NavVisibilityContext = {
      isSystemAdmin: false,
      userRole: 'viewer',
      // Permissions are deliberately empty: the JWT carries no permissions
      // claim, so a permission-gated sidebar item would be invisible to all.
      userPermissions: [],
      devMode: false,
      tierInfoLoaded: true,
      isAtMinimumTier: () => true,
      isDemoSession: false,
      isFeatureEnabled: () => false,
    }

    const visiblePaths = (role: string) =>
      getVisibleNavGroups({ ...baseContext, userRole: role })
        .flatMap((group) => group.items)
        .map((item) => item.to)

    for (const role of roles) {
      expect(visiblePaths(role)).toContain('/settings/notifications')
    }
  })
})
