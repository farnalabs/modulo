import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const routeState = vi.hoisted(() => ({
  params: { id: 'err-1' } as Record<string, string>,
}))

const { fetchGroupMock, updateGroupMock, fetchEventsMock, getMock } = vi.hoisted(() => ({
  fetchGroupMock: vi.fn(),
  updateGroupMock: vi.fn(),
  fetchEventsMock: vi.fn(),
  getMock: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/admin/errors/err-1',
    fullPath: '/admin/errors/err-1',
    params: routeState.params,
    query: {},
    hash: '',
    matched: [],
    name: 'admin-error-detail',
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn() })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../lib/api/errors', () => ({
  fetchErrorGroup: fetchGroupMock,
  updateErrorGroup: updateGroupMock,
  fetchErrorGroupEvents: fetchEventsMock,
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock, POST: vi.fn(), PUT: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminErrorDetailView from '../views/AdminErrorDetailView.vue'

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
    routeState.params = { id: 'err-1' }
    fetchGroupMock.mockResolvedValue(groupDetail())
    fetchEventsMock.mockResolvedValue(eventsPage([eventRow()], 1))
    getMock.mockResolvedValue({
      data: { items: [{ id: 'u1', email: 'a@b.c', display_name: 'Alice' }] },
      error: undefined,
    })
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
    // Known repo-wide ErrorAlert bug: the Retry button does not render for an
    // absent `retryable` Boolean prop (characterised in AdminAuditView.spec.ts).
    expect(alert.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(0)
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
})
