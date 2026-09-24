import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const h = vi.hoisted(() => ({
  mockFetchNotifications: vi.fn(),
  mockFetchUnreadCount: vi.fn(),
  mockRegisterHandler: vi.fn((_event: string, _cb: () => void) => vi.fn()),
  mockOnReconnect: vi.fn((_cb: () => void) => vi.fn()),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchNotifications: h.mockFetchNotifications,
  reviewLater: vi.fn(),
  fetchUnreadCount: h.mockFetchUnreadCount,
  dismissNotification: vi.fn(),
}))

vi.mock('../stores/syncRegistry', () => ({
  registerHandler: h.mockRegisterHandler,
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : 'Unknown error'),
}))

vi.mock('@vueuse/core', async () => {
  const { ref } = await import('vue')
  return { useStorage: (_key: string, defaultValue: boolean) => ref(defaultValue) }
})

// The component only consumes eventBus.onReconnect; stub it so the reconnect
// backfill callback (FAR-250) can be captured and invoked directly.
vi.mock('../composables/useEventStream', () => ({
  eventBus: {
    onReconnect: (cb: () => void) => h.mockOnReconnect(cb),
  },
}))

import DashboardNotificationsPanel from '../components/DashboardNotificationsPanel.vue'

describe('DashboardNotificationsPanel reconnect backfill (FAR-250)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    h.mockFetchNotifications.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 })
    h.mockFetchUnreadCount.mockResolvedValue(0)
  })

  it('REST-backfills the list after a successful SSE reconnect', async () => {
    mount(DashboardNotificationsPanel)
    await flushPromises()

    expect(h.mockOnReconnect).toHaveBeenCalledTimes(1)
    h.mockFetchNotifications.mockClear()

    const backfill = h.mockOnReconnect.mock.calls[0][0] as () => void
    backfill()
    await flushPromises()

    expect(h.mockFetchNotifications).toHaveBeenCalledTimes(1)
  })

  it('unmounts before the reconnect handler is registered without throwing', async () => {
    // Unmount in the same tick as mount, before the async onMounted body has
    // assigned unsubReconnect: onUnmounted must tolerate the null handler.
    const wrapper = mount(DashboardNotificationsPanel)
    wrapper.unmount()
    await flushPromises()

    expect(wrapper.exists()).toBe(false)
  })
})
