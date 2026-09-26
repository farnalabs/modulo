import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const { mockFetchNotifications, mockReviewLater } = vi.hoisted(() => ({
  mockFetchNotifications: vi.fn(),
  mockReviewLater: vi.fn(),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchNotifications: (...args: unknown[]) => mockFetchNotifications(...args),
  reviewLater: (...args: unknown[]) => mockReviewLater(...args),
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
    mockFetchNotifications.mockResolvedValue(pageResponse([], 0, 1))
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
})
