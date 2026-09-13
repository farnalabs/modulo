import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick, ref } from 'vue'

const { mockFetchDashboard, mockReviewLater, mockRegisterHandler } = vi.hoisted(() => ({
  mockFetchDashboard: vi.fn(),
  mockReviewLater: vi.fn(),
  mockRegisterHandler: vi.fn(() => vi.fn()),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchDashboardNotifications: mockFetchDashboard,
  reviewLater: mockReviewLater,
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

async function mountAndExpand(notifications: unknown[] = [], unreadCount = 0) {
  mockFetchDashboard.mockResolvedValue({ notifications, total_unread: unreadCount })
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
      [{ id: 'n-1', title: 'Test', level: 'info' }],
      1,
    )

    const link = wrapper.find('a[href="/notifications"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('View all notifications')
  })
})
