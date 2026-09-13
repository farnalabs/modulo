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

  it('expands a delivery row via keyboard Enter for a11y (FAR-821)', async () => {
    const entry = {
      id: 'd-1',
      status: 'failed',
      last_error: 'boom-details',
      response_code: 500,
      response_body: null,
      endpoint_url: 'https://example.com/hook',
      endpoint_id: 'ep-1',
      created_at: '2025-06-30T12:00:00Z',
    }
    ;(api.GET as never as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: { items: [entry], total: 1, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()
    const row = wrapper.find('tbody tr')
    expect(row.exists()).toBe(true)

    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.text()).toContain('boom-details')
  })

  it('expands a delivery row via keyboard Space for a11y (FAR-821)', async () => {
    const entry = {
      id: 'd-2',
      status: 'failed',
      last_error: 'space-details',
      response_code: 500,
      response_body: null,
      endpoint_url: 'https://example.com/hook',
      endpoint_id: 'ep-2',
      created_at: '2025-06-30T12:00:00Z',
    }
    ;(api.GET as never as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: { items: [entry], total: 1, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()
    const row = wrapper.find('tbody tr')
    expect(row.exists()).toBe(true)

    await row.trigger('keydown', { key: ' ', code: 'Space' })
    await nextTick()
    expect(wrapper.text()).toContain('space-details')
  })
})
