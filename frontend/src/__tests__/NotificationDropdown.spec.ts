import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { NotificationResponse } from '../lib/api/notifications'

const h = vi.hoisted(() => ({
  mockFetchDashboard: vi.fn(),
  mockFetchUnreadCount: vi.fn(),
  mockMarkAllRead: vi.fn(),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchDashboardNotifications: h.mockFetchDashboard,
  fetchUnreadCount: h.mockFetchUnreadCount,
  markAllNotificationsRead: h.mockMarkAllRead,
}))

import NotificationDropdown from '../components/NotificationDropdown.vue'
import { useNotificationsStore, DROPDOWN_LIMIT } from '../stores/notifications'

function notification(overrides: Partial<NotificationResponse> = {}): NotificationResponse {
  return {
    id: 'n-1',
    scope: 'org',
    level: 'warning',
    category: 'run.failed',
    title: 'Run failed',
    body: 'Pipeline "Improve Security" failed.',
    action_url: '/runs/abc',
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    created_at: '2026-09-23T12:00:00+00:00',
    scope_label: 'Org-wide',
    run_terminal: false,
    ...overrides,
  }
}

async function mountDropdown() {
  const wrapper = mount(NotificationDropdown, {
    global: {
      plugins: [createPinia()],
      // A default VTU stub renders no slot content and no href, which hides
      // the row text and the view-all target; this stub mirrors what the real
      // RouterLink exposes to tests.
      stubs: {
        'router-link': {
          props: { to: { type: [String, Object], default: '' } },
          template: '<a :href="typeof to === \'string\' ? to : undefined"><slot /></a>',
        },
      },
    },
  })
  await flushPromises()
  return wrapper
}

describe('NotificationDropdown (FAR-249)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    h.mockFetchDashboard.mockResolvedValue({ notifications: [], total_unread: 0 })
    h.mockFetchUnreadCount.mockResolvedValue(0)
    h.mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 2 })
  })

  it('fetches recent notifications on mount and renders them', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification(), notification({ id: 'n-2', title: 'HITL overdue', level: 'error' })],
      total_unread: 2,
    })

    const wrapper = await mountDropdown()

    expect(h.mockFetchDashboard).toHaveBeenCalledWith(DROPDOWN_LIMIT)
    const items = wrapper.findAll('[data-testid="notification-dropdown"] li')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('Run failed')
    expect(items[1].text()).toContain('HITL overdue')
  })

  it('renders the empty state when there are no notifications', async () => {
    const wrapper = await mountDropdown()
    expect(wrapper.find('[data-testid="notification-dropdown-empty"]').exists()).toBe(true)
  })

  it('falls back to an empty list when the payload omits notifications', async () => {
    // `notifications` is optional on the dashboard payload; a missing field
    // must resolve to the empty list (nullish fallback) rather than leaving
    // stale entries, so the dropdown shows the empty state.
    h.mockFetchDashboard.mockResolvedValue({ total_unread: 0 })
    const store = useNotificationsStore()

    await store.fetchRecent()

    expect(store.recentNotifications).toEqual([])
  })

  it('renders an error state, NOT the empty state, when the recent fetch fails', async () => {
    h.mockFetchDashboard.mockRejectedValue(new Error('boom'))

    const wrapper = await mountDropdown()

    // The error state must replace the empty state: "No notifications" would
    // falsely claim the feed is clear when the fetch simply failed.
    expect(wrapper.find('[data-testid="notification-dropdown-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notification-dropdown-error"]').attributes('role')).toBe('alert')
    expect(wrapper.find('[data-testid="notification-dropdown-empty"]').exists()).toBe(false)
  })

  it('keeps the previous list and badge when a later fetchRecent fails', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification()],
      total_unread: 1,
    })
    const wrapper = await mountDropdown()
    const store = useNotificationsStore()
    expect(store.unreadCount).toBe(1)

    h.mockFetchDashboard.mockRejectedValue(new Error('transient'))
    await store.fetchRecent()
    await flushPromises()

    // Fail-open: the earlier items and badge survive the failed refresh.
    expect(store.recentNotifications).toHaveLength(1)
    expect(store.unreadCount).toBe(1)
    expect(store.fetchError).toBeTruthy()
    // With items still held, the list stays visible — the error region only
    // replaces the EMPTY state.
    expect(wrapper.findAll('[data-testid="notification-dropdown"] li')).toHaveLength(1)
    expect(wrapper.find('[data-testid="notification-dropdown-error"]').exists()).toBe(false)

    // A later successful fetch clears the error flag.
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification()],
      total_unread: 1,
    })
    await store.fetchRecent()
    expect(store.fetchError).toBeNull()
  })

  it('retries the failed fetch from the error state', async () => {
    h.mockFetchDashboard.mockRejectedValue(new Error('boom'))
    const wrapper = await mountDropdown()
    const retry = wrapper.find('[data-testid="notification-dropdown-retry"]')
    expect(retry.exists()).toBe(true)

    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification()],
      total_unread: 1,
    })
    await retry.trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="notification-dropdown-error"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="notification-dropdown"] li')).toHaveLength(1)
  })

  it('renders a dialog with an accessible title', async () => {
    const wrapper = await mountDropdown()
    const dialog = wrapper.find('[data-testid="notification-dropdown"]')
    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-label')).toBeTruthy()
    expect(dialog.attributes('id')).toBe('notification-dropdown')
  })

  it('marks all read via the store, clears the list, and announces success', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification()],
      total_unread: 1,
    })
    const wrapper = await mountDropdown()
    // After mark-all-read the follow-up fetches return an empty feed.
    h.mockFetchDashboard.mockResolvedValue({ notifications: [], total_unread: 0 })
    h.mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 1 })

    await wrapper.find('[data-testid="notification-mark-all-read"]').trigger('click')
    await flushPromises()

    expect(h.mockMarkAllRead).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notification-mark-all-read-success"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notification-dropdown-empty"]').exists()).toBe(true)

    const store = useNotificationsStore()
    expect(store.unreadCount).toBe(0)
  })

  it('disables the mark-all-read button when the unread count is zero', async () => {
    const wrapper = await mountDropdown()
    const button = wrapper.find('[data-testid="notification-mark-all-read"]')
    expect(button.attributes('disabled')).toBeDefined()
  })

  it('surfaces a failure alert and keeps the list when mark-all-read fails', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification()],
      total_unread: 1,
    })
    const wrapper = await mountDropdown()
    h.mockMarkAllRead.mockRejectedValue(new Error('boom'))

    await wrapper.find('[data-testid="notification-mark-all-read"]').trigger('click')
    await flushPromises()

    const alert = wrapper.find('[data-testid="notification-mark-all-read-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.attributes('role')).toBe('alert')
    // The list is untouched — the clear did not happen.
    expect(wrapper.findAll('[data-testid="notification-dropdown"] li')).toHaveLength(1)
  })

  it('emits close when a notification link is clicked', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification()],
      total_unread: 1,
    })
    const wrapper = await mountDropdown()

    // The stub renders the row as an <a>; click the link itself (clicking the
    // <li> would not reach the link's listener).
    await wrapper.find('[data-testid="notification-dropdown"] li a').trigger('click')
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('emits close on Escape', async () => {
    const wrapper = await mountDropdown()
    await wrapper.find('[data-testid="notification-dropdown"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('renders a view-all link to the notifications page', async () => {
    const wrapper = await mountDropdown()
    const link = wrapper.find('[data-testid="notification-dropdown-view-all"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/notifications')
  })

  it('renders notifications without an action_url as non-link rows', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [notification({ action_url: null })],
      total_unread: 1,
    })
    const wrapper = await mountDropdown()
    const row = wrapper.find('[data-testid="notification-dropdown"] li')
    expect(row.find('a').exists()).toBe(false)
    expect(row.text()).toContain('Run failed')
  })

  it('emits close when the view-all link is clicked', async () => {
    const wrapper = await mountDropdown()
    await wrapper.find('[data-testid="notification-dropdown-view-all"]').trigger('click')
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('renders the info and fallback level-dot colours', async () => {
    h.mockFetchDashboard.mockResolvedValue({
      notifications: [
        notification({ id: 'n-info', level: 'info' }),
        notification({ id: 'n-unknown', level: 'debug' }),
      ],
      total_unread: 2,
    })
    const wrapper = await mountDropdown()
    const dots = wrapper.findAll('[data-testid="notification-dropdown"] li span.rounded-full')
    expect(dots).toHaveLength(2)
    // Distinct classes → both the `info` arm and the `default` arm executed.
    expect(dots[0].classes()).not.toEqual(dots[1].classes())
  })
})
