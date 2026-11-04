import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const routeState = vi.hoisted(() => ({
  params: { id: 'err-1' } as Record<string, string>,
  query: {} as Record<string, unknown>,
}))

const authState = vi.hoisted(() => ({ isSystemAdmin: false }))
const routerPush = vi.hoisted(() => vi.fn())

vi.mock('../composables/useCurrentUser', async () => {
  const { ref } = await import('vue')
  return {
    useCurrentUser: () => ({ isSystemAdmin: ref(authState.isSystemAdmin) }),
  }
})

const { fetchGroupMock, updateGroupMock, fetchEventsMock, getMock, fetchInstanceGroupMock, fetchInstanceEventsMock } = vi.hoisted(() => ({
  fetchGroupMock: vi.fn(),
  updateGroupMock: vi.fn(),
  fetchEventsMock: vi.fn(),
  getMock: vi.fn(),
  fetchInstanceGroupMock: vi.fn(),
  fetchInstanceEventsMock: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/admin/errors/err-1',
    fullPath: '/admin/errors/err-1',
    params: routeState.params,
    query: routeState.query,
    hash: '',
    matched: [],
    name: 'admin-error-detail',
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({ push: routerPush, replace: vi.fn() })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../lib/api/errors', () => ({
  fetchErrorGroup: fetchGroupMock,
  updateErrorGroup: updateGroupMock,
  fetchErrorGroupEvents: fetchEventsMock,
  // FAR-1555: the instance-scope reads are hoisted into the API layer too, so
  // they are mocked at the same boundary as their tenant counterparts.
  fetchInstanceErrorGroup: fetchInstanceGroupMock,
  fetchInstanceErrorGroupEvents: fetchInstanceEventsMock,
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock, POST: vi.fn(), PUT: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminErrorDetailView from '../views/AdminErrorDetailView.vue'
import BackLink from '../components/BackLink.vue'

function groupDetail(over: Record<string, unknown> = {}) {
  return {
    id: 'err-1',
    fingerprint: 'abcdef1234567890',
    level_peak: 'error',
    status: 'new',
    count: 3,
    first_seen: '2026-08-01T10:00:00Z',
    last_seen: '2026-08-02T12:00:00Z',
    assigned_to: null,
    sample_event: {
      id: 'evt-1',
      message: 'TypeError: x is not a function',
      stacktrace: 'at fn (app.js:1)',
      context_json: { pipeline_id: 'p1' },
      source: 'runner',
      environment: 'production',
      version: '1.2.3',
      level: 'error',
      created_at: '2026-08-02T12:00:00Z',
    },
    ...over,
  }
}

function eventsPage(items: Record<string, unknown>[], total: number) {
  return { items, total }
}

const ADMIN_PERMISSIONS = ['errors.resolve', 'errors.manage']
const VIEWER_PERMISSIONS = ['errors.resolve']

/**
 * Boot-time `api.GET` surface for the view: the caller-scoped permission read
 * that gates every write control (FAR-2374) and the admin-only user lookup
 * behind the assignee picker. Anything else keeps the permissive stub the
 * suite has always used.
 */
function grantPermissions(permissions: string[]) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/auth/permissions') {
      return Promise.resolve({ data: { permissions }, error: undefined })
    }
    if (path === '/api/v1/admin/users') {
      return Promise.resolve({
        data: { items: [{ id: 'u1', email: 'a@b.c', display_name: 'Alice' }] },
        error: undefined,
      })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  })
}

/** Every path the view (or a store it boots) asked `api.GET` for. */
function requestedPaths(): string[] {
  return getMock.mock.calls.map((call) => String(call[0]))
}

async function flush() {
  await flushPromises()
  await nextTick()
}

function mountView() {
  return mount(AdminErrorDetailView, {
    global: {
      stubs: {
        LoadingSpinner: true,
        JsonViewer: true,
      },
    },
  })
}

describe('AdminErrorDetailView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    authState.isSystemAdmin = false
    routeState.params = { id: 'err-1' }
    routeState.query = {}
    routerPush.mockClear()
    fetchGroupMock.mockResolvedValue(groupDetail())
    fetchEventsMock.mockResolvedValue(eventsPage([eventRow()], 1))
    fetchInstanceGroupMock.mockResolvedValue(groupDetail())
    fetchInstanceEventsMock.mockResolvedValue(eventsPage([eventRow()], 1))
    // The suite's default principal holds `errors.manage`; the viewer state is
    // exercised by the dedicated FAR-2374 tests below.
    grantPermissions(ADMIN_PERMISSIONS)
  })

  function eventRow(over: Record<string, unknown> = {}) {
    return {
      id: 'evt-1',
      level: 'error',
      message: 'TypeError: x is not a function',
      source: 'runner',
      environment: 'production',
      version: '1.2.3',
      created_at: '2026-08-02T12:00:00Z',
      ...over,
    }
  }

  it('renders group summary cards, sample event and raw events after load', async () => {
    const wrapper = mountView()
    await flush()

    expect(fetchGroupMock).toHaveBeenCalledWith('err-1')
    expect(wrapper.text()).toContain('error') // level_peak
    expect(wrapper.text()).toContain('new') // status
    expect(wrapper.text()).toContain('3') // occurrences
    expect(wrapper.text()).toContain('TypeError: x is not a function')
    expect(wrapper.text()).toContain('runner')
    expect(wrapper.text()).toContain('production')
    expect(wrapper.text()).toContain('1.2.3')
    expect(wrapper.text()).toContain('Raw Events (1)')
    expect(wrapper.text()).toContain('Fingerprint: #abcdef12')
    expect(wrapper.text()).toContain('Inspect occurrences, status, ownership, and event context for this error group.')
  })

  it('raw events: pagination next/prev fetch successive offsets and disable at bounds', async () => {
    fetchEventsMock.mockResolvedValue(eventsPage([eventRow()], 45))
    const wrapper = mountView()
    await flush()

    const buttons = wrapper.findAll('button')
    const prev = buttons.find((b) => b.text().trim() === 'Previous')!
    const next = buttons.find((b) => b.text().trim() === 'Next')!
    expect(prev.attributes('disabled')).toBeDefined()
    expect(next.attributes('disabled')).toBeUndefined()
    expect(fetchEventsMock).toHaveBeenCalledWith('err-1', { limit: 20, offset: 0 })

    await next.trigger('click')
    await flush()
    expect(fetchEventsMock).toHaveBeenLastCalledWith('err-1', { limit: 20, offset: 20 })
  })

  it('raw events: empty page shows the no-events message instead of the list', async () => {
    fetchEventsMock.mockResolvedValue(eventsPage([], 0))
    const wrapper = mountView()
    await flush()

    expect(wrapper.text()).toContain('No raw events loaded')
    expect(wrapper.text()).toContain('Raw Events (0)')
  })

  it('status actions: acknowledge disabled when already acknowledged; resolve PUTs and reloads', async () => {
    fetchGroupMock.mockResolvedValue(groupDetail({ status: 'acknowledged' }))
    updateGroupMock.mockResolvedValue(groupDetail({ status: 'resolved' }))
    const wrapper = mountView()
    await flush()

    const buttons = wrapper.findAll('button')
    const acknowledge = buttons.find((b) => b.text().trim() === 'Acknowledge')!
    const resolve = buttons.find((b) => b.text().trim() === 'Resolve')!
    expect(acknowledge.attributes('disabled')).toBeDefined()
    expect(resolve.attributes('disabled')).toBeUndefined()

    await resolve.trigger('click')
    await flush()

    expect(updateGroupMock).toHaveBeenCalledWith('err-1', { status: 'resolved' })
    expect(fetchGroupMock).toHaveBeenCalledTimes(2)
  })

  it('assignee: mounted group assigns the loaded assignee and saving sends the PATCH body', async () => {
    fetchGroupMock.mockResolvedValue(groupDetail({ assigned_to: 'u1' }))
    updateGroupMock.mockResolvedValue(groupDetail())
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as { assigneeId: string; updateAssignee: () => Promise<void> }
    expect(vm.assigneeId).toBe('u1')

    vm.assigneeId = 'u2'
    await vm.updateAssignee()
    expect(updateGroupMock).toHaveBeenLastCalledWith('err-1', { assigned_to: 'u2' })

    vm.assigneeId = ''
    await vm.updateAssignee()
    expect(updateGroupMock).toHaveBeenLastCalledWith('err-1', { assigned_to: undefined })
  })

  it('stacktrace and context sections toggle open on click', async () => {
    const wrapper = mountView()
    await flush()

    const buttons = wrapper.findAll('button')
    const stackBtn = buttons.find((b) => b.text().includes('Stacktrace'))!
    expect(wrapper.find('pre').exists()).toBe(false)
    await stackBtn.trigger('click')
    await nextTick()
    const pre = wrapper.find('pre')
    expect(pre.exists()).toBe(true)
    expect(pre.text()).toContain('at fn (app.js:1)')

    const contextBtn = wrapper.findAll('button').find((b) => b.text().includes('Context'))!
    expect(wrapper.find('json-viewer-stub').exists()).toBe(false)
    await contextBtn.trigger('click')
    await nextTick()
    expect(wrapper.find('json-viewer-stub').exists()).toBe(true)
  })

  it('group fetch failure surfaces the inline ErrorAlert with the formatted error', async () => {
    fetchGroupMock.mockRejectedValue(new Error('group gone'))
    const wrapper = mountView()
    await flush()

    const alert = wrapper.find('.border-destructive\\/50')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load error group')
    expect(alert.text()).toContain('group gone')
    // ErrorAlert defaults `retryable` to true, so a load failure with an
    // on-retry handler (loadDetail) offers the Retry action. Characterised in
    // AdminAuditView.spec.ts (FAR-608 fix).
    expect(alert.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
    wrapper.unmount()
  })

  it('events fetch failure surfaces the failure to load events message', async () => {
    fetchEventsMock.mockRejectedValue(new Error('events boom'))
    const wrapper = mountView()
    await flush()

    expect(wrapper.text()).toContain('Failed to load events')
    expect(wrapper.text()).toContain('events boom')
  })

  it('status update failure surfaces the failure to update status message', async () => {
    updateGroupMock.mockRejectedValue(new Error('status boom'))
    const wrapper = mountView()
    await flush()

    const resolve = wrapper.findAll('button').find((b) => b.text().trim() === 'Resolve')!
    await resolve.trigger('click')
    await flush()

    expect(wrapper.text()).toContain('Failed to update status')
    expect(wrapper.text()).toContain('status boom')
  })

  it('missing group (null) renders header only without detail cards', async () => {
    fetchGroupMock.mockResolvedValue(null as unknown as Record<string, unknown>)
    const wrapper = mountView()
    await flush()

    expect(wrapper.text()).toContain('Error Group Detail')
    expect(wrapper.text()).not.toContain('Acknowledge')
  })

  it('instance scope reads the sentinel endpoints, renders read-only and skips tenant actions (FAR-1547)', async () => {
    authState.isSystemAdmin = true
    routeState.query = { scope: 'instance' }
    const wrapper = mountView()
    await flush()

    // The instance reads are hoisted into the API layer (FAR-1555); the path
    // each helper hits is pinned in errors.spec.ts.
    expect(fetchInstanceGroupMock).toHaveBeenCalledWith('err-1')
    expect(fetchInstanceEventsMock).toHaveBeenCalledWith('err-1', { limit: 20, offset: 0 })
    expect(fetchGroupMock).not.toHaveBeenCalled()
    expect(fetchEventsMock).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="admin-error-detail-instance-readonly"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Acknowledge')
    expect(wrapper.find('[data-testid="admin-error-detail-resolve"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-error-detail-archive"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-error-detail-assignee"]').exists()).toBe(false)
    // The assignee picker lives in the tenant-only actions card, so the
    // instance view never fires the admin user lookup behind it.
    expect(getMock).not.toHaveBeenCalledWith('/api/v1/admin/users')
    wrapper.unmount()
  })

  it('a forged ?scope=instance without the system-admin claim stays on the tenant detail (FAR-1555)', async () => {
    authState.isSystemAdmin = false
    routeState.query = { scope: 'instance' }
    const wrapper = mountView()
    await flush()

    expect(fetchGroupMock).toHaveBeenCalledWith('err-1')
    expect(fetchInstanceGroupMock).not.toHaveBeenCalled()
    expect(fetchInstanceEventsMock).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="admin-error-detail-instance-readonly"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-error-detail-acknowledge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-error-detail-assignee"]').exists()).toBe(true)
    expect(wrapper.findComponent(BackLink).props('to')).toBe('/admin/errors')
    wrapper.unmount()
  })

  it('instance scope back link and goBack carry the scope query (FAR-1547)', async () => {
    authState.isSystemAdmin = true
    routeState.query = { scope: 'instance' }
    const wrapper = mountView()
    await flush()

    expect(wrapper.findComponent(BackLink).props('to')).toBe('/admin/errors?scope=instance')
    const vm = wrapper.vm as unknown as { goBack: () => void }
    vm.goBack()
    expect(routerPush).toHaveBeenCalledWith({ path: '/admin/errors', query: { scope: 'instance' } })
    wrapper.unmount()
  })

  it('organisation scope goBack returns to the dashboard without a scope query (FAR-1547)', async () => {
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as { goBack: () => void }
    vm.goBack()
    expect(routerPush).toHaveBeenCalledWith('/admin/errors')
    wrapper.unmount()
  })

  it('copies the canonical support reference including the group source (FAR-2304)', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = mountView()
    await flush()

    const btn = wrapper.find('[data-testid="admin-error-detail-copy-support"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flush()

    expect(writeText).toHaveBeenCalledOnce()
    // The detail payload's sample event carries the source the list payload
    // cannot, so the reference here is the full canonical shape.
    expect(writeText.mock.calls[0][0]).toBe(
      'error err-1 · fingerprint abcdef1234567890 · level error · count 3 · source runner',
    )
    wrapper.unmount()
  })

  it('offers the support reference in instance scope too (read-only copy)', async () => {
    authState.isSystemAdmin = true
    routeState.query = { scope: 'instance' }
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="admin-error-detail-copy-support"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('warns and keeps the copy label when the clipboard write fails (FAR-2304)', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.assign(navigator, { clipboard: { writeText } })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = mountView()
    await flush()

    const btn = wrapper.find('[data-testid="admin-error-detail-copy-support"]')
    await btn.trigger('click')
    await flush()

    expect(warn).toHaveBeenCalledWith('Failed to copy support reference', expect.any(Error))
    expect(btn.text()).toContain('Copy support reference')
    warn.mockRestore()
    wrapper.unmount()
  })

  it('has no reference and copies nothing before a group loads (FAR-2304)', async () => {
    fetchGroupMock.mockResolvedValue(null)
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as {
      supportReference: string
      copySupportReference: () => Promise<void>
    }
    expect(vm.supportReference).toBe('')
    await vm.copySupportReference()
    expect(writeText).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('omits the source segment when the sample event has no source (FAR-2304)', async () => {
    fetchGroupMock.mockResolvedValue(groupDetail({ sample_event: null }))
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as { supportReference: string }
    expect(vm.supportReference).toContain('error err-1')
    expect(vm.supportReference).not.toContain('source')
    wrapper.unmount()
  })

  it('viewer: every write control is disabled and described, and the admin user lookup is never fired (FAR-2374)', async () => {
    grantPermissions(VIEWER_PERMISSIONS)
    const wrapper = mountView()
    await flush()

    // Status transitions: the PATCH is admin-only, so all three are disabled
    // even though the group's own status would otherwise enable them.
    for (const testid of [
      'admin-error-detail-acknowledge',
      'admin-error-detail-resolve',
      'admin-error-detail-archive',
    ]) {
      const button = wrapper.find(`[data-testid="${testid}"]`)
      expect(button.exists()).toBe(true)
      expect(button.attributes('disabled')).toBeDefined()
      expect(button.attributes('aria-describedby')).toBe('admin-error-status-admin-help')
    }
    const statusHelp = wrapper.find('[data-testid="admin-error-detail-status-admin-help"]')
    expect(statusHelp.exists()).toBe(true)
    expect(statusHelp.text()).toContain('administrator')

    // Reassignment: the picker is disabled and greyed, described through the
    // combobox itself (PrimeVue puts fallthrough attrs on its wrapper).
    const assignee = wrapper.find('[data-testid="admin-error-detail-assignee"]')
    expect(assignee.exists()).toBe(true)
    const combobox = assignee.find('[role="combobox"]')
    expect(combobox.attributes('aria-disabled')).toBe('true')
    expect(combobox.attributes('tabindex')).toBe('-1')
    expect(combobox.attributes('aria-describedby')).toBe('admin-error-assign-admin-help')
    const assignHelp = wrapper.find('[data-testid="admin-error-detail-assign-admin-help"]')
    expect(assignHelp.exists()).toBe(true)
    expect(assignHelp.text()).toContain('administrator')
    expect(assignee.element.closest('.opacity-60')).not.toBeNull()

    // GET /api/v1/admin/users is admin-only: a viewer must not ask for it at
    // all, rather than firing it and swallowing the 403.
    expect(requestedPaths()).not.toContain('/api/v1/admin/users')
    wrapper.unmount()
  })

  it('admin: the write controls stay enabled and the assignee picker loads users (FAR-2374)', async () => {
    grantPermissions(ADMIN_PERMISSIONS)
    const wrapper = mountView()
    await flush()

    for (const testid of [
      'admin-error-detail-acknowledge',
      'admin-error-detail-resolve',
      'admin-error-detail-archive',
    ]) {
      const button = wrapper.find(`[data-testid="${testid}"]`)
      expect(button.attributes('disabled')).toBeUndefined()
      expect(button.attributes('aria-describedby')).toBeUndefined()
    }
    expect(wrapper.find('[data-testid="admin-error-detail-status-admin-help"]').exists()).toBe(false)

    const assignee = wrapper.find('[data-testid="admin-error-detail-assignee"]')
    const combobox = assignee.find('[role="combobox"]')
    expect(combobox.attributes('aria-disabled')).not.toBe('true')
    expect(combobox.attributes('aria-describedby')).toBeUndefined()
    expect(wrapper.find('[data-testid="admin-error-detail-assign-admin-help"]').exists()).toBe(false)
    expect(assignee.element.closest('.opacity-60')).toBeNull()

    expect(requestedPaths()).toContain('/api/v1/admin/users')
    wrapper.unmount()
  })
})
