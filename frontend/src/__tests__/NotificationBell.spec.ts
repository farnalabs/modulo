import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const h = vi.hoisted(() => ({
  mockFetchUnreadCount: vi.fn(),
  mockApiGet: vi.fn(),
  mockSubscribe: vi.fn((_type: string, _cb: (e: unknown) => void) => vi.fn()),
  mockOnReconnect: vi.fn((_cb: () => void) => vi.fn()),
  mockReconnect: vi.fn(),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchUnreadCount: h.mockFetchUnreadCount,
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: h.mockApiGet },
}))

vi.mock('../lib/api/formatError', () => ({
  throwOnError: (result: { data?: unknown; error?: unknown }) => {
    if (result.error) throw new Error('prefs failed')
    return result.data
  },
}))

// Reactive banner flag: a plain { value } object would NOT trigger a re-render
// (Vue only tracks refs/computed), so the mock owns a real ref and the test
// flips it through the exported __banner handle.
vi.mock('../composables/useEventStream', async () => {
  const { ref } = await import('vue')
  const banner = ref(false)
  return {
    __banner: banner,
    eventBus: {
      get reconnectRequired() {
        return banner.value
      },
      get connected() {
        return false
      },
      get state() {
        return 'idle'
      },
      subscribe: (type: string, cb: (e: unknown) => void) => h.mockSubscribe(type, cb),
      onReconnect: (cb: () => void) => h.mockOnReconnect(cb),
      reconnect: () => h.mockReconnect(),
      unsubscribe: vi.fn(),
    },
  }
})

import NotificationBell from '../components/NotificationBell.vue'
import * as eventStreamModule from '../composables/useEventStream'

const bannerFlag = (eventStreamModule as unknown as { __banner: { value: boolean } }).__banner

function notificationEvent(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'notification',
    id: 'n-1',
    action: 'created',
    version: 1,
    org_id: 'org-1',
    notification_id: 'n-1',
    category: 'run_failed',
    created_at: '2026-09-23T12:00:00+00:00',
    ...overrides,
  }
}

async function mountBell() {
  const wrapper = mount(NotificationBell)
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('NotificationBell (FAR-250)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    bannerFlag.value = false
    h.mockFetchUnreadCount.mockResolvedValue(3)
    h.mockApiGet.mockResolvedValue({
      data: {
        dashboard_level: 'warning',
        notification_opt_outs: { run_failed: true, hitl_overdue: false },
      },
      error: undefined,
    })
  })

  it('fetches notification preferences BEFORE subscribing to the stream', async () => {
    await mountBell()

    expect(h.mockApiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app/preferences')
    expect(h.mockSubscribe).toHaveBeenCalledTimes(1)
    // invocationCallOrder proves prefs loaded before the first SSE subscription
    // (read-time suppression is active from the first event).
    const prefsOrder = h.mockApiGet.mock.invocationCallOrder[0]
    const subscribeOrder = h.mockSubscribe.mock.invocationCallOrder[0]
    expect(prefsOrder).toBeLessThan(subscribeOrder)
  })

  it('subscribes to the notification resource type (retains the shared stream)', async () => {
    await mountBell()
    expect(h.mockSubscribe.mock.calls[0][0]).toBe('notification')
    expect(h.mockSubscribe.mock.calls[0][1]).toBeTypeOf('function')
  })

  it('renders the unread badge from the initial fetch', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(7)
    const wrapper = await mountBell()
    const badge = wrapper.find('[data-testid="notification-unread-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('7')
  })

  it('refetches the unread count when a non-opted-out notification event arrives', async () => {
    const wrapper = await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()
    h.mockFetchUnreadCount.mockResolvedValue(4)

    handler(notificationEvent({ category: 'hitl_overdue' }))
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notification-unread-badge"]').text()).toBe('4')
  })

  it('applies read-time suppression: no refetch for an opted-out category', async () => {
    await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: 'run_failed' })) // opted out in the prefs mock
    await flushPromises()

    expect(h.mockFetchUnreadCount).not.toHaveBeenCalled()
  })

  it('tolerates events without a category field (generic envelope) by refetching', async () => {
    await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: undefined }))
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
  })

  it('registers an onReconnect backfill that refetches the unread count', async () => {
    await mountBell()
    expect(h.mockOnReconnect).toHaveBeenCalledTimes(1)
    const backfill = h.mockOnReconnect.mock.calls[0][0] as () => void
    h.mockFetchUnreadCount.mockClear()

    backfill()
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
  })

  it('fails open when the preferences fetch fails (empty opt-out set)', async () => {
    h.mockApiGet.mockResolvedValue({ data: undefined, error: { detail: 'boom' } })
    const wrapper = await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: 'run_failed' }))
    await flushPromises()

    // Suppression set is empty -> refetch proceeds (server still filters reads).
    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notification-unread-badge"]').exists()).toBe(true)
  })

  it('shows the reconnect banner when the stream stopped on a 4xx, and restarts on click', async () => {
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="sse-reconnect-banner"]').exists()).toBe(false)

    bannerFlag.value = true
    await nextTick()

    const banner = wrapper.find('[data-testid="sse-reconnect-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('status')
    expect(banner.attributes('aria-live')).toBe('polite')

    await wrapper.find('[data-testid="sse-reconnect-button"]').trigger('click')
    expect(h.mockReconnect).toHaveBeenCalledTimes(1)

    bannerFlag.value = false
    await nextTick()
    expect(wrapper.find('[data-testid="sse-reconnect-banner"]').exists()).toBe(false)
  })

  it('does not render a badge when the unread count is zero', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(0)
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="notification-unread-badge"]').exists()).toBe(false)
  })
})
