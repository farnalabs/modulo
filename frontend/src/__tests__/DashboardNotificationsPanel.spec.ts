import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick, ref } from 'vue'

const { mockFetchNotifications, mockReviewLater, mockFetchUnreadCount, mockRegisterHandler, mockDismissNotification } = vi.hoisted(() => ({
  mockFetchNotifications: vi.fn(),
  mockReviewLater: vi.fn(),
  mockFetchUnreadCount: vi.fn(),
  mockRegisterHandler: vi.fn((_event: string, _cb: () => void) => vi.fn()),
  mockDismissNotification: vi.fn(),
}))

type DNPVm = {
  notifications: Array<{ id: string }>
  total: number
  reviewLaterError: string
  page: number
  totalPages: number
  nextPage: () => void
  prevPage: () => void
  onReviewLater: (id: string) => Promise<void>
  onDismissed: (id: string) => Promise<void>
  refreshUnreadCount: () => Promise<void>
  unreadCount: number
  error: string | null
}

vi.mock('../lib/api/notifications', () => ({
  fetchNotifications: mockFetchNotifications,
  reviewLater: mockReviewLater,
  fetchUnreadCount: mockFetchUnreadCount,
  dismissNotification: mockDismissNotification,
}))

vi.mock('../stores/syncRegistry', () => ({
  registerHandler: mockRegisterHandler,
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : 'Unknown error'),
}))

vi.mock('@vueuse/core', () => ({
  useStorage: (_key: string, defaultValue: boolean) => {
    return ref(defaultValue)
  },
}))

import DashboardNotificationsPanel from '../components/DashboardNotificationsPanel.vue'

function makeNotifications(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    id: `n-${i + 1}`,
    title: `Notification ${i + 1}`,
    level: 'info',
    category: 'pipeline_run',
    scope: 'org',
    body: `Body ${i + 1}`,
    action_url: null,
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    created_at: new Date().toISOString(),
    scope_label: 'Organization',
    run_id: null,
    run_status: null,
    run_terminal: false,
    run_cancel_reason: null,
  }))
}

async function mountAndExpand(notifications: unknown[] = [], total = 0, unreadCount = 0) {
  mockFetchNotifications.mockResolvedValue({
    items: notifications,
    total,
    page: 1,
    page_size: 10,
  })
  mockFetchUnreadCount.mockResolvedValue(unreadCount)
  const wrapper = mount(DashboardNotificationsPanel)
  await flushPromises()
  await nextTick()

  // Panel is collapsed by default — click toggle to expand
  const toggle = wrapper.find('[data-testid="notifications-panel-toggle"]')
  await toggle.trigger('click')
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('DashboardNotificationsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchUnreadCount.mockResolvedValue(0)
  })

  it('always renders the "View all notifications" link when expanded', async () => {
    const wrapper = await mountAndExpand([], 0)

    const link = wrapper.find('a[href="/notifications"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('View all notifications')
  })

  it('renders the link when notifications list is empty', async () => {
    const wrapper = await mountAndExpand([], 0)

    const link = wrapper.find('a[href="/notifications"]')
    expect(link.exists()).toBe(true)
  })

  it('renders the link when notifications are present', async () => {
    const wrapper = await mountAndExpand(
      [{ id: 'n-1', title: 'Test', level: 'info', category: 'pipeline_run', scope: 'org', body: '', action_url: null, dismiss_strategy: 'user_only', dismissible_at_scope: false, created_at: '2025-06-01T10:00:00Z', scope_label: 'Organization', run_id: null, run_status: null, run_terminal: false, run_cancel_reason: null }],
      1,
    )

    const link = wrapper.find('a[href="/notifications"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('View all notifications')
  })

  it('calls fetchNotifications with status=active and page_size=10', async () => {
    await mountAndExpand([], 0)

    expect(mockFetchNotifications).toHaveBeenCalledWith({
      page: 1,
      page_size: 10,
      status: 'active',
    })
  })

  it('shows paging controls when total pages > 1', async () => {
    const notifications = makeNotifications(12)
    const wrapper = await mountAndExpand(notifications, 12)

    const prevBtn = wrapper.find('[data-testid="panel-prev-page"]')
    const nextBtn = wrapper.find('[data-testid="panel-next-page"]')
    expect(prevBtn.exists()).toBe(true)
    expect(nextBtn.exists()).toBe(true)
    // First page: prev disabled, next enabled
    expect((prevBtn.element as HTMLButtonElement).disabled).toBe(true)
    expect((nextBtn.element as HTMLButtonElement).disabled).toBe(false)
  })

  it('does not show paging controls when all items fit on one page', async () => {
    const notifications = makeNotifications(5)
    const wrapper = await mountAndExpand(notifications, 5)

    expect(wrapper.find('[data-testid="panel-prev-page"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="panel-next-page"]').exists()).toBe(false)
  })

  it('has aria-expanded on the toggle button', async () => {
    const wrapper = await mountAndExpand([], 0)

    const toggle = wrapper.find('[data-testid="notifications-panel-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('true')
  })

  it('sets aria-live="polite" on the unread count badge', async () => {
    mockFetchNotifications.mockResolvedValue({
      items: [],
      total: 3,
      page: 1,
      page_size: 10,
    })
    mockFetchUnreadCount.mockResolvedValue(3)
    const wrapper = mount(DashboardNotificationsPanel)
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[role="status"]')
    expect(badge.exists()).toBe(true)
  })

  it('uses the severity-filtered unread-count endpoint for the badge, not the raw total of active notifications', async () => {
    const notifications = makeNotifications(12)
    // 12 active notifications in total, but only 2 are warning+ (the unread-count endpoint)
    mockFetchNotifications.mockResolvedValue({
      items: notifications,
      total: 12,
      page: 1,
      page_size: 10,
    })
    mockFetchUnreadCount.mockResolvedValue(2)
    const wrapper = mount(DashboardNotificationsPanel)
    await flushPromises()
    await nextTick()

    // Badge reflects the severity-filtered unread count (2), not the raw total (12)
    expect(mockFetchUnreadCount).toHaveBeenCalled()
    const badge = wrapper.find('[role="status"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('2')
  })

  it('onReviewLater removes the notification, decrements the total, and refreshes the unread count', async () => {
    mockReviewLater.mockResolvedValue(undefined)
    mockFetchUnreadCount.mockResolvedValue(2)
    const wrapper = await mountAndExpand(makeNotifications(3), 3, 3)
    const vm = wrapper.vm as unknown as DNPVm
    await vm.onReviewLater('n-2')
    await flushPromises()
    await nextTick()
    expect(mockReviewLater).toHaveBeenCalledWith('n-2')
    expect(vm.notifications.find((n) => n.id === 'n-2')).toBeUndefined()
    expect(vm.total).toBe(2)
    expect(mockFetchUnreadCount).toHaveBeenCalled()
  })

  it('onReviewLater sets an error when the API rejects', async () => {
    mockReviewLater.mockRejectedValue(new Error('boom'))
    const wrapper = await mountAndExpand(makeNotifications(3), 3, 3)
    const vm = wrapper.vm as unknown as DNPVm
    await vm.onReviewLater('n-1')
    await flushPromises()
    await nextTick()
    expect(vm.reviewLaterError).toBe('boom')
  })

  it('onDismissed removes the notification and refreshes the unread count', async () => {
    mockDismissNotification.mockResolvedValue(undefined)
    mockFetchUnreadCount.mockResolvedValue(2)
    const wrapper = await mountAndExpand(makeNotifications(3), 3, 3)
    const vm = wrapper.vm as unknown as DNPVm
    await vm.onDismissed('n-3')
    await flushPromises()
    await nextTick()
    expect(mockDismissNotification).not.toHaveBeenCalled()
    expect(vm.notifications.find((n) => n.id === 'n-3')).toBeUndefined()
    expect(vm.total).toBe(2)
    expect(mockFetchUnreadCount).toHaveBeenCalled()
  })

  it('onDismissed on the last page goes back a page when the list empties', async () => {
    mockDismissNotification.mockResolvedValue(undefined)
    mockFetchNotifications.mockResolvedValue({ items: [], total: 1, page: 1, page_size: 10 })
    const wrapper = await mountAndExpand(makeNotifications(1), 1, 1)
    const vm = wrapper.vm as unknown as DNPVm
    // Simulate being on page 2 with a single (now-removed) item.
    vm.page = 2
    vm.total = 1
    vm.notifications = makeNotifications(1)
    await vm.onDismissed('n-1')
    await flushPromises()
    await nextTick()
    expect(vm.page).toBe(1)
    expect(mockFetchNotifications).toHaveBeenLastCalledWith({ page: 1, page_size: 10, status: 'active' })
  })

  it('nextPage advances the page and reloads when not on the last page', async () => {
    const wrapper = await mountAndExpand(makeNotifications(12), 20)
    const vm = wrapper.vm as unknown as DNPVm
    vm.page = 1
    vm.nextPage()
    await flushPromises()
    await nextTick()
    expect(vm.page).toBe(2)
    expect(mockFetchNotifications).toHaveBeenLastCalledWith({ page: 2, page_size: 10, status: 'active' })
  })

  it('prevPage goes back a page and reloads when past the first page', async () => {
    const wrapper = await mountAndExpand(makeNotifications(12), 20)
    const vm = wrapper.vm as unknown as DNPVm
    vm.page = 2
    vm.prevPage()
    await flushPromises()
    await nextTick()
    expect(vm.page).toBe(1)
    expect(mockFetchNotifications).toHaveBeenLastCalledWith({ page: 1, page_size: 10, status: 'active' })
  })

  it('does not advance past the last page', async () => {
    const wrapper = await mountAndExpand(makeNotifications(12), 20)
    const vm = wrapper.vm as unknown as DNPVm
    vm.page = vm.totalPages
    vm.nextPage()
    await flushPromises()
    await nextTick()
    expect(vm.page).toBe(vm.totalPages)
  })

  it('reloads when the sync registry fires a "notification" event', async () => {
    await mountAndExpand([], 0)
    const handler = mockRegisterHandler.mock.calls[0]?.[1] as undefined | (() => void)
    expect(handler).toBeTypeOf('function')
    mockFetchNotifications.mockClear()
    ;(handler as () => void)()
    await flushPromises()
    await nextTick()
    expect(mockFetchNotifications).toHaveBeenCalledWith({ page: 1, page_size: 10, status: 'active' })
  })

  it('sets an error when loadPage fails', async () => {
    mockFetchNotifications.mockRejectedValue(new Error('network down'))
    const wrapper = mount(DashboardNotificationsPanel)
    await flushPromises()
    await nextTick()
    const vm = wrapper.vm as unknown as DNPVm
    expect(vm.error).toBe('network down')
  })

  it('unsubscribes the sync handler on unmount', async () => {
    const unsub = vi.fn()
    mockRegisterHandler.mockReturnValueOnce(unsub)
    const wrapper = await mountAndExpand([], 0)
    wrapper.unmount()
    expect(unsub).toHaveBeenCalled()
  })

  it('refreshUnreadCount resets the badge to 0 when the endpoint fails', async () => {
    const wrapper = await mountAndExpand(makeNotifications(3), 3, 3)
    const vm = wrapper.vm as unknown as DNPVm
    mockFetchUnreadCount.mockRejectedValue(new Error('badge down'))
    await vm.refreshUnreadCount()
    await flushPromises()
    await nextTick()
    expect(vm.unreadCount).toBe(0)
  })

  it('onReviewLater on the last page goes back a page when the list empties', async () => {
    mockReviewLater.mockResolvedValue(undefined)
    mockFetchNotifications.mockResolvedValue({ items: [], total: 1, page: 1, page_size: 10 })
    const wrapper = await mountAndExpand(makeNotifications(1), 1, 1)
    const vm = wrapper.vm as unknown as DNPVm
    vm.page = 2
    vm.total = 1
    vm.notifications = makeNotifications(1)
    await vm.onReviewLater('n-1')
    await flushPromises()
    await nextTick()
    expect(vm.page).toBe(1)
    expect(mockFetchNotifications).toHaveBeenLastCalledWith({ page: 1, page_size: 10, status: 'active' })
  })
})
