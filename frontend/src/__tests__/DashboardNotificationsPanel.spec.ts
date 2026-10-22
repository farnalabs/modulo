import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const { mockFetchNotifications, mockReviewLater, mockFetchUnreadCount, mockRegisterHandler, mockDismissNotification, mockMarkAllRead, mockFetchDashboardNotifications } = vi.hoisted(() => ({
  mockFetchNotifications: vi.fn(),
  mockReviewLater: vi.fn(),
  mockFetchUnreadCount: vi.fn(),
  mockRegisterHandler: vi.fn((_event: string, _cb: () => void) => vi.fn()),
  mockDismissNotification: vi.fn(),
  mockMarkAllRead: vi.fn(),
  mockFetchDashboardNotifications: vi.fn(),
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
  // The shared notifications store (FAR-1926 dismissAll) also imports these
  // two — the panel now routes its "Dismiss all" through that store action.
  markAllNotificationsRead: mockMarkAllRead,
  fetchDashboardNotifications: mockFetchDashboardNotifications,
}))

vi.mock('../stores/syncRegistry', () => ({
  registerHandler: mockRegisterHandler,
}))

vi.mock('../lib/api/formatError', async (importOriginal) => {
  // Re-export the real module (it has no imports of its own) so any component
  // mounted here — e.g. the notifications page imported for the cross-surface
  // assertion below — can resolve names like isProblemDetail, and override
  // only the formatter under test.
  const actual = await importOriginal<typeof import('../lib/api/formatError')>()
  return {
    ...actual,
    formatApiError: (e: unknown) => (e instanceof Error ? e.message : 'Unknown error'),
  }
})

vi.mock('@vueuse/core', () => ({
  useStorage: (_key: string, defaultValue: boolean) => {
    return ref(defaultValue)
  },
}))

import DashboardNotificationsPanel from '../components/DashboardNotificationsPanel.vue'
// QA FAR-1926 — cross-surface assertion: a failure raised on the panel must
// not render on a DIFFERENT mounted surface (the notifications page).
import NotificationsPage from '../views/NotificationsPage.vue'
import { useNotificationsStore } from '../stores/notifications'

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
    setActivePinia(createPinia())
    mockFetchUnreadCount.mockResolvedValue(0)
    // The panel now uses the shared notifications store (FAR-1926); give the
    // store's own two API legs sensible defaults so unrelated tests never hit
    // an unconfigured mock.
    mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 0 })
    mockFetchDashboardNotifications.mockResolvedValue({ notifications: [], total_unread: 0 })
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

    const badge = wrapper.find('output')
    expect(badge.exists()).toBe(true)
    // Native <output> carries the implicit status role; no explicit role is
    // allowed back (SonarCloud Web:S6819).
    expect(badge.attributes('role')).toBeUndefined()
    // WCAG AA: 11px bold text is not "large text"; the fill-safe destructive
    // token is required for `--destructive-foreground` to clear 4.5:1.
    expect(badge.classes()).toContain('bg-destructive-fill')
    expect(badge.classes()).toContain('text-destructive-foreground')
    expect(badge.classes()).not.toContain('bg-destructive')
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
    const badge = wrapper.find('output')
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

  // FAR-1926 — Dismiss all in the expanded-content header.
  it('renders Dismiss all in the expanded content, disabled when there is nothing to dismiss', async () => {
    const wrapper = await mountAndExpand([], 0)

    const button = wrapper.find('[data-testid="panel-dismiss-all"]')
    expect(button.exists()).toBe(true)
    expect(button.text()).toBe('Dismiss all')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('Dismiss all posts the shared per-user bulk action, then reloads list and badge from the server post-state', async () => {
    const wrapper = await mountAndExpand(makeNotifications(3), 3, 3)
    const button = wrapper.find('[data-testid="panel-dismiss-all"]')
    expect((button.element as HTMLButtonElement).disabled).toBe(false)

    // The server post-state after the write: nothing active, badge 0.
    mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 3 })
    mockFetchNotifications.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 })
    mockFetchUnreadCount.mockResolvedValue(0)

    await button.trigger('click')
    await flushPromises()
    await nextTick()

    // The ONE shared action path — the store's dismissAll — hit the endpoint.
    expect(mockMarkAllRead).toHaveBeenCalledTimes(1)
    // The panel re-reads its OWN list + badge from the server (not a local clear).
    expect(mockFetchNotifications).toHaveBeenLastCalledWith({ page: 1, page_size: 10, status: 'active' })
    expect(mockFetchUnreadCount).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="panel-dismiss-all-success"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="panel-dismiss-all-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No notifications')

    const vm = wrapper.vm as unknown as DNPVm
    expect(vm.unreadCount).toBe(0)
  })

  it('Dismiss all failure surfaces a role=alert and never shows a falsely cleared list', async () => {
    const wrapper = await mountAndExpand(makeNotifications(2), 2, 2)
    mockMarkAllRead.mockRejectedValue(new Error('boom'))
    mockFetchNotifications.mockClear()

    await wrapper.find('[data-testid="panel-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()

    const alert = wrapper.find('[data-testid="panel-dismiss-all-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.attributes('role')).toBe('alert')
    // The store throws an Error carrying the FORMATTED API message, and the
    // panel renders its own local ref (never a shared store error).
    expect(alert.text()).toBe('boom')
    expect(wrapper.find('[data-testid="panel-dismiss-all-success"]').exists()).toBe(false)
    // The list is untouched — no clear, no post-failure reload.
    expect(wrapper.findAll('.notification-card')).toHaveLength(2)
    expect(mockFetchNotifications).not.toHaveBeenCalled()
  })

  // QA FAR-1926 — truthful copy: the SERVER's marked count picks the outcome.
  it('shows "Nothing to dismiss" instead of a success claim when the server marks zero rows', async () => {
    const wrapper = await mountAndExpand(makeNotifications(2), 2, 2)
    mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 0 })

    await wrapper.find('[data-testid="panel-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(mockMarkAllRead).toHaveBeenCalledTimes(1)
    const nothing = wrapper.find('[data-testid="panel-dismiss-all-nothing"]')
    expect(nothing.exists()).toBe(true)
    expect(nothing.text()).toBe('Nothing to dismiss')
    expect(nothing.attributes('aria-live')).toBe('polite')
    expect(wrapper.find('[data-testid="panel-dismiss-all-success"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="panel-dismiss-all-error"]').exists()).toBe(false)
  })

  // QA FAR-1926 — the dismiss-all error is LOCAL to the surface that raised
  // it (the store holds no error state any more) and retires on that
  // surface's own next successful attempt.
  it('scopes a dismiss-all failure to this surface: another mounted surface never renders it, and it clears on the next success', async () => {
    const wrapper = await mountAndExpand(makeNotifications(2), 2, 2)
    mockMarkAllRead.mockRejectedValue(new Error('boom'))

    await wrapper.find('[data-testid="panel-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="panel-dismiss-all-error"]').exists()).toBe(true)

    // A DIFFERENT surface mounting on the SAME store must not render the
    // stale error (pre-fix the shared store ref made the page show it).
    mockFetchNotifications.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
    const page = mount(NotificationsPage)
    await flushPromises()
    await nextTick()
    expect(page.find('[data-testid="notifications-dismiss-all-error"]').exists()).toBe(false)
    page.unmount()

    // The local error retires on this surface's own next successful attempt.
    mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 2 })
    mockFetchNotifications.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 })
    mockFetchUnreadCount.mockResolvedValue(0)
    await wrapper.find('[data-testid="panel-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="panel-dismiss-all-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="panel-dismiss-all-success"]').exists()).toBe(true)
  })

  it('renders a non-Error rejection through String() when dismiss-all fails unexpectedly', async () => {
    // Defensive arm of `e instanceof Error ? e.message : String(e)`: the shared
    // store action normally throws an Error, but a non-Error throw (e.g. a
    // rejected string) must still surface a readable message.
    const wrapper = await mountAndExpand(makeNotifications(2), 2, 2)
    const store = useNotificationsStore()
    vi.spyOn(store, 'dismissAll').mockRejectedValue('string failure')

    await wrapper.find('[data-testid="panel-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()

    const alert = wrapper.find('[data-testid="panel-dismiss-all-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toBe('string failure')
    expect(wrapper.find('[data-testid="panel-dismiss-all-success"]').exists()).toBe(false)
  })
})
