import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { api } from '../lib/api/client'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [], total: 0, next_cursor: null }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminNotificationDeliveryLogView from '../views/AdminNotificationDeliveryLogView.vue'

describe('AdminNotificationDeliveryLogView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Webhook Notifications')
  })

  it('renders empty state when no deliveries', async () => {
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No delivery logs found')
  })

  it('renders filter controls', async () => {
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await nextTick()
    expect(wrapper.find('[data-testid="admin-notification-log-status"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-event-type"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-date-from"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-date-to"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-apply"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-reset"]').exists()).toBe(true)
  })

  it('renders pagination controls when items exist', async () => {
    const { api } = await import('../lib/api/client')
    const mockGet = (api as any).GET as ReturnType<typeof vi.fn>
    mockGet.mockResolvedValue({
      data: {
        items: [
          {
            id: '1',
            event_type: 'run_failed',
            status: 'failed',
            attempt_count: 3,
            response_code: 500,
            last_error: 'Internal server error',
            response_body: null,
            endpoint_url: 'https://example.com/hook',
            endpoint_id: 'ep-1',
            created_at: '2025-06-30T12:00:00Z',
          },
        ],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="admin-notification-log-previous"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-next"]').exists()).toBe(true)
  })

  it('expands a delivery row via keyboard (Enter and Space) for a11y', async () => {
    const { api } = await import('../lib/api/client')
    const mockGet = (api as any).GET as ReturnType<typeof vi.fn>
    mockGet.mockResolvedValue({
      data: {
        items: [
          {
            id: '1',
            event_type: 'run_failed',
            status: 'failed',
            attempt_count: 3,
            response_code: 500,
            last_error: 'Internal server error',
            response_body: null,
            endpoint_url: 'https://example.com/hook',
            endpoint_id: 'ep-1',
            created_at: '2025-06-30T12:00:00Z',
          },
        ],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()

    const row = wrapper.find('tr[tabindex="0"]')
    expect(row.exists()).toBe(true)

    // Enter toggles the row expansion open (detail row with colspan=8).
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('td[colspan="8"]').exists()).toBe(true)

    // Enter again collapses it.
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('td[colspan="8"]').exists()).toBe(false)

    // Space re-opens the expansion (keyboard-only equivalent of the click).
    await row.trigger('keydown', { key: ' ' })
    await nextTick()
    expect(wrapper.find('td[colspan="8"]').exists()).toBe(true)
  })
})
