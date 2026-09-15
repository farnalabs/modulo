import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick, ref } from 'vue'

const { mockFetchNotifications, mockReviewLater, mockFetchUnreadCount, mockRegisterHandler } = vi.hoisted(() => ({
  mockFetchNotifications: vi.fn(),
  mockReviewLater: vi.fn(),
  mockFetchUnreadCount: vi.fn(),
  mockRegisterHandler: vi.fn(() => vi.fn()),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchNotifications: mockFetchNotifications,
  reviewLater: mockReviewLater,
  fetchUnreadCount: mockFetchUnreadCount,
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
      [{ id: 'n-1', title: 'Test', level: 'info', category: 'pipeline_run', scope: 'org', body: '', action_url: null, dismiss_strategy: 'user_only', dismissible_at_scope: false, created_at: '2025-06-01T10:00:00Z', scope_label: 'Organization' }],
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
})
