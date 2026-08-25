import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import RemyChat from '../components/remy/RemyChat.vue'
import { useRemyStore } from '../composables/useRemyStore'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    POST: vi.fn(() => Promise.resolve({ error: null, data: {} })),
    GET: vi.fn(() => Promise.resolve({ error: null, data: { items: [] } })),
    PATCH: vi.fn(() => Promise.resolve({ error: null, data: {} })),
    DELETE: vi.fn(() => Promise.resolve({ error: null, data: {} })),
  },
}))

vi.mock('@/stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    featureEnabled: vi.fn((name: string) => name === 'remy_ui_driving'),
  })),
}))

vi.mock('@/composables/useRemyStream', () => ({
  useRemyStream: vi.fn(() => ({
    connectStream: vi.fn(() => Promise.resolve()),
    disconnectStream: vi.fn(() => Promise.resolve()),
    connected: { value: false },
  })),
}))

vi.mock('@/composables/useUiCommandExecutor', () => ({
  pauseUiCommands: vi.fn(),
  resumeUiCommands: vi.fn(),
  abortUiCommands: vi.fn(),
  executeCommandBatch: vi.fn(),
  isPaused: vi.fn(() => false),
}))

vi.mock('vue-echarts', () => ({
  default: { name: 'VChart', props: ['option'], template: '<div class="vchart-stub" />' },
}))
vi.mock('echarts', () => ({ default: {} }))

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

function mountChat(remyOnly: boolean) {
  return mount(RemyChat, { props: { remyOnly } })
}

async function triggerDeleteFlow(wrapper: ReturnType<typeof mountChat>) {
  await wrapper.find('.remy-input').setValue('/delete')
  await wrapper.find('.remy-input').trigger('keydown', { key: 'Enter' })
  await wrapper.find('.remy-delete-confirm button').trigger('click')
  await flushPromises()
}

describe('RemyChat remyOnly prop', () => {
  it('hides the permission/NOGO card when remyOnly, even with a pending permission', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    }
    const wrapper = mountChat(true)
    expect(wrapper.find('.remy-permission-card').exists()).toBe(false)
  })

  it('still renders the permission card when NOT remyOnly (panel regression)', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    }
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-permission-card').exists()).toBe(true)
  })

  it('does NOT auto-create a new session after deleting the last session in remy-only mode', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.sessions = []
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)
    const loadSpy = vi.spyOn(store, 'loadSession').mockResolvedValue(undefined as never)

    const wrapper = mountChat(true)
    await triggerDeleteFlow(wrapper)

    expect(createSpy).not.toHaveBeenCalled()
    expect(loadSpy).not.toHaveBeenCalled()
  })

  it('auto-creates a new session after deleting the last session in panel mode (regression)', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.sessions = []
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)

    const wrapper = mountChat(false)
    await triggerDeleteFlow(wrapper)

    expect(createSpy).toHaveBeenCalled()
  })

  it('does NOT render the UI-executing indicator when remyOnly', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const wrapper = mountChat(true)
    expect(wrapper.find('.remy-executing-indicator').exists()).toBe(false)
  })

  it('renders the UI-executing indicator when NOT remyOnly (panel regression)', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-executing-indicator').exists()).toBe(true)
  })
})

describe('RemyChat analytics chart card', () => {
  it('renders a chart card + deep link for a successful query_analytics tool result', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-1',
      tool_name: 'query_analytics',
      success: true,
      result: {
        group_by: 'day',
        dimension: null,
        date_from: '2026-07-30',
        date_to: '2026-08-06',
        deep_link: '/analytics?group_by=day&date_from=2026-07-30&date_to=2026-08-06',
        buckets: [
          { date: '2026-08-01', count: 3 },
          { date: '2026-08-02', count: 5 },
        ],
      },
    })
    const wrapper = mountChat(false)
    await flushPromises()
    expect(wrapper.find('[data-testid="remy-analytics-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-chart"]').exists()).toBe(true)
    const link = wrapper.find('.remy-analytics-link')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/analytics?group_by=day&date_from=2026-07-30&date_to=2026-08-06')
  })

  it('falls back to the generic tool card when the analytics result is not chartable', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-2',
      tool_name: 'query_analytics',
      success: true,
      result: { group_by: 'day', buckets: 'not-an-array' },
    })
    const wrapper = mountChat(false)
    expect(wrapper.find('[data-testid="remy-analytics-card"]').exists()).toBe(false)
    expect(wrapper.find('.remy-tool-card').exists()).toBe(true)
  })
})
