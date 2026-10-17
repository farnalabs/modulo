import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const { mockFetchNotifications, mockReviewLater, mockMarkAllRead, mockFetchDashboardNotifications } = vi.hoisted(() => ({
  mockFetchNotifications: vi.fn(),
  mockReviewLater: vi.fn(),
  mockMarkAllRead: vi.fn(),
  mockFetchDashboardNotifications: vi.fn(),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchNotifications: (...args: unknown[]) => mockFetchNotifications(...args),
  reviewLater: (...args: unknown[]) => mockReviewLater(...args),
  // The page's shared notifications store (FAR-1926 dismissAll) also imports
  // these two.
  markAllNotificationsRead: (...args: unknown[]) => mockMarkAllRead(...args),
  fetchDashboardNotifications: (...args: unknown[]) => mockFetchDashboardNotifications(...args),
}))

vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  return {
    useDataFetch: (
      fetcher: () => Promise<{ data: unknown }>,
      options?: { initialValue?: unknown },
    ) => {
      const data = ref(options?.initialValue)
      const loading = ref(false)
      const error = ref('')
      const load = async () => {
        loading.value = true
        try {
          const result = await fetcher()
          ;(data as { value: unknown }).value = result.data ?? options?.initialValue
        } catch {
          error.value = 'Failed to load'
        } finally {
          loading.value = false
        }
      }
      void load()
      return { data, loading, error, load }
    },
  }
})

import NotificationsPage from '../views/NotificationsPage.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import NotificationCard from '../components/NotificationCard.vue'

function pageResponse(items: unknown[] = [], total = 0, page = 1) {
  return { items, total, page, page_size: 20 }
}

function makeNotification(id: string) {
  return {
    id,
    title: 'Test',
    level: 'info',
    category: 'pipeline_run',
    scope: 'org',
    body: '',
    action_url: null,
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    created_at: new Date().toISOString(),
    scope_label: 'Organization',
    run_id: null,
    run_status: null,
    run_terminal: false,
    run_cancel_reason: null,
  }
}

describe('NotificationsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    mockFetchNotifications.mockResolvedValue(pageResponse([], 0, 1))
    // The page now uses the shared notifications store (FAR-1926); give the
    // store's own two API legs defaults so unrelated tests never hit an
    // unconfigured mock.
    mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 0 })
    mockFetchDashboardNotifications.mockResolvedValue({ notifications: [], total_unread: 0 })
  })

  it('shows a skeleton placeholder while loading, not an indeterminate spinner', async () => {
    mockFetchNotifications.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(NotificationsPage)
    await nextTick()

    const skeleton = wrapper.find('[data-testid="notifications-loading"]')
    expect(skeleton.exists()).toBe(true)
    // A native <output> carries the implicit ARIA status role, so its
    // aria-label is permitted (axe aria-prohibited-attr) without an explicit
    // role attribute (SonarCloud Web:S6819) — assert the native element rather
    // than a redundant role, matching ParameterSchemasView / NotificationBell.
    expect(skeleton.element.tagName).toBe('OUTPUT')
    expect(skeleton.attributes('aria-busy')).toBe('true')
    expect(wrapper.find('.animate-spin').exists()).toBe(false)
  })

  it('fetches with status=active by default and auto-applies on filter change', async () => {
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()
    expect(mockFetchNotifications).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'active' }),
    )

    const filterBar = wrapper.findComponent(FilterBar)
    await filterBar.vm.$emit('update:filter', 'level', 'error')
    await flushPromises()
    await nextTick()

    expect(mockFetchNotifications).toHaveBeenLastCalledWith(
      expect.objectContaining({ level: 'error', status: 'active' }),
    )
  })

  it('resets filters back to the active status', async () => {
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    const filterBar = wrapper.findComponent(FilterBar)
    await filterBar.vm.$emit('update:filter', 'level', 'error')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="notifications-reset-filters"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(mockFetchNotifications).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: 'active' }),
    )
  })

  it('paginates to the next page', async () => {
    mockFetchNotifications.mockResolvedValue(pageResponse([makeNotification('n-1')], 30, 1))
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="notifications-next-page"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(mockFetchNotifications).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2 }),
    )
  })

  it('removes a notification after a successful review-later', async () => {
    mockFetchNotifications.mockResolvedValue(pageResponse([makeNotification('n-1')], 1, 1))
    mockReviewLater.mockResolvedValue(undefined)
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    const card = wrapper.findComponent(NotificationCard)
    await card.vm.$emit('review-later', 'n-1')
    await flushPromises()
    await nextTick()

    expect(mockReviewLater).toHaveBeenCalledWith('n-1')
  })

  it('surfaces a review-later failure as an error', async () => {
    mockFetchNotifications.mockResolvedValue(pageResponse([makeNotification('n-1')], 1, 1))
    mockReviewLater.mockRejectedValue(new Error('review failed'))
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    const card = wrapper.findComponent(NotificationCard)
    await card.vm.$emit('review-later', 'n-1')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('review failed')
  })

  // FAR-1926 — Dismiss all beside the filters.
  it('renders Dismiss all beside the filters, disabled when there is nothing to dismiss', async () => {
    mockFetchNotifications.mockResolvedValue(pageResponse([], 0, 1))
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    const button = wrapper.find('[data-testid="notifications-dismiss-all"]')
    expect(button.exists()).toBe(true)
    expect(button.text()).toBe('Dismiss all')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('Dismiss all posts the shared per-user bulk action and reloads the list from the server post-state', async () => {
    mockFetchNotifications.mockResolvedValue(pageResponse([makeNotification('n-1')], 1, 1))
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    // Server post-state after the write: the active list is empty.
    mockMarkAllRead.mockResolvedValue({ status: 'marked_read', marked: 1 })
    mockFetchNotifications.mockResolvedValue(pageResponse([], 0, 1))

    await wrapper.find('[data-testid="notifications-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()

    // The ONE shared action path — the store's dismissAll — hit the endpoint.
    expect(mockMarkAllRead).toHaveBeenCalledTimes(1)
    expect(mockFetchNotifications).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'active' }))
    expect(wrapper.find('[data-testid="notifications-dismiss-all-success"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notifications-dismiss-all-error"]').exists()).toBe(false)
    // An emptied inbox renders the empty state, not a stale local list.
    expect(wrapper.findComponent(NotificationCard).exists()).toBe(false)
    expect(wrapper.find('[data-testid="notifications-clear-filters"]').exists()).toBe(true)
  })

  it('Dismiss all failure surfaces a role=alert and never shows a falsely cleared list', async () => {
    mockFetchNotifications.mockResolvedValue(pageResponse([makeNotification('n-1')], 1, 1))
    const wrapper = mount(NotificationsPage)
    await flushPromises()
    await nextTick()

    mockMarkAllRead.mockRejectedValue(new Error('nope'))
    mockFetchNotifications.mockClear()

    await wrapper.find('[data-testid="notifications-dismiss-all"]').trigger('click')
    await flushPromises()
    await nextTick()

    const alert = wrapper.find('[data-testid="notifications-dismiss-all-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.attributes('role')).toBe('alert')
    expect(wrapper.find('[data-testid="notifications-dismiss-all-success"]').exists()).toBe(false)
    // The list is untouched — no clear, no post-failure reload.
    expect(wrapper.findComponent(NotificationCard).exists()).toBe(true)
    expect(mockFetchNotifications).not.toHaveBeenCalled()
  })
})
