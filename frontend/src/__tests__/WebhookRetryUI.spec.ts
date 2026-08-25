import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn().mockResolvedValue({ data: { items: [], total: 0, next_cursor: null }, error: undefined }),
  mockPost: vi.fn().mockResolvedValue({ data: null, error: undefined }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: mockPost,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminNotificationDeliveryLogView from '../views/AdminNotificationDeliveryLogView.vue'

const failedEntry = {
  id: 'dlv-fail-1',
  event_type: 'run_failed',
  status: 'failed',
  attempt_count: 3,
  response_code: 500,
  last_error: 'Internal server error',
  response_body: null,
  endpoint_url: 'https://example.com/hook',
  endpoint_id: 'ep-1',
  created_at: '2025-06-30T12:00:00Z',
}

const deadLetteredEntry = {
  id: 'dlv-dl-1',
  event_type: 'hitl_awaiting',
  status: 'dead_lettered',
  attempt_count: 5,
  response_code: null,
  last_error: 'Connection refused',
  response_body: null,
  endpoint_url: 'https://example.com/dead',
  endpoint_id: 'ep-2',
  created_at: '2025-06-30T13:00:00Z',
}

const deliveredEntry = {
  id: 'dlv-ok-1',
  event_type: 'claim_expired',
  status: 'delivered',
  attempt_count: 1,
  response_code: 200,
  last_error: null,
  response_body: '{"ok":true}',
  endpoint_url: 'https://example.com/hook',
  endpoint_id: 'ep-1',
  created_at: '2025-06-30T11:00:00Z',
}

const pendingEntry = {
  id: 'dlv-pending-1',
  event_type: 'hitl_overdue',
  status: 'pending',
  attempt_count: 0,
  response_code: null,
  last_error: null,
  response_body: null,
  endpoint_url: 'https://example.com/hook',
  endpoint_id: 'ep-1',
  created_at: '2025-06-30T10:00:00Z',
}

function mountWithItems(items: any[]) {
  mockGet.mockResolvedValue({
    data: { items, total: items.length, next_cursor: null },
    error: undefined,
  })
  const wrapper = mount(AdminNotificationDeliveryLogView)
  return wrapper
}

describe('WebhookRetryUI', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  describe('per-row Retry button', () => {
    it('renders Retry button for failed entries', async () => {
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const buttons = wrapper.findAll('[data-testid="admin-notification-log-retry"]')
      expect(buttons).toHaveLength(1)
      expect(buttons[0].text()).toBe('Retry')
    })

    it('renders Retry button for dead_lettered entries', async () => {
      const wrapper = mountWithItems([deadLetteredEntry])
      await flushPromises()
      await nextTick()
      const buttons = wrapper.findAll('[data-testid="admin-notification-log-retry"]')
      expect(buttons).toHaveLength(1)
    })

    it('does not render Retry button for delivered entries', async () => {
      const wrapper = mountWithItems([deliveredEntry])
      await flushPromises()
      await nextTick()
      const buttons = wrapper.findAll('[data-testid="admin-notification-log-retry"]')
      expect(buttons).toHaveLength(0)
    })

    it('does not render Retry button for pending entries', async () => {
      const wrapper = mountWithItems([pendingEntry])
      await flushPromises()
      await nextTick()
      const buttons = wrapper.findAll('[data-testid="admin-notification-log-retry"]')
      expect(buttons).toHaveLength(0)
    })

    it('shows Retrying… text while retry is in progress', async () => {
      mockPost.mockImplementationOnce(() => new Promise(() => {})) // never resolves
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry"]')
      await btn.trigger('click')
      await nextTick()
      expect(btn.text()).toBe('Retrying…')
    })

    it('shows success message after successful retry', async () => {
      mockPost.mockResolvedValue({ data: { success: true, status_code: 200 }, error: undefined })
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry"]')
      await btn.trigger('click')
      await flushPromises()
      await nextTick()
      expect(wrapper.text()).toContain('Retry succeeded')
    })

    it('shows error message after failed retry', async () => {
      mockPost.mockResolvedValue({ data: { success: false, status_code: 500, error: 'HTTP 500' }, error: undefined })
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry"]')
      await btn.trigger('click')
      await flushPromises()
      await nextTick()
      expect(wrapper.text()).toContain('Retry failed')
    })

    it('shows error message when endpoint_id is missing', async () => {
      const noEndpoint = { ...failedEntry, endpoint_id: null }
      const wrapper = mountWithItems([noEndpoint])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry"]')
      await btn.trigger('click')
      await nextTick()
      expect(wrapper.text()).toContain('Cannot retry: missing endpoint ID')
    })

    it('disables button while retrying', async () => {
      mockPost.mockImplementationOnce(() => new Promise(() => {}))
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry"]')
      await btn.trigger('click')
      await nextTick()
      expect(btn.attributes('disabled')).toBeDefined()
    })
  })

  describe('Retry All Failed button', () => {
    it('shows Retry All Failed button when there are failed entries', async () => {
      const wrapper = mountWithItems([failedEntry, deliveredEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      expect(btn.exists()).toBe(true)
      expect(btn.text()).toBe('Retry All Failed')
    })

    it('shows Retry All Failed button when there are dead_lettered entries', async () => {
      const wrapper = mountWithItems([deadLetteredEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      expect(btn.exists()).toBe(true)
    })

    it('hides Retry All Failed button when all entries are delivered/pending', async () => {
      const wrapper = mountWithItems([deliveredEntry, pendingEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      expect(btn.exists()).toBe(false)
    })

    it('shows Retrying All… text while retry-all is in progress', async () => {
      mockPost.mockImplementationOnce(() => new Promise(() => {}))
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      await btn.trigger('click')
      await nextTick()
      expect(btn.text()).toBe('Retrying All…')
    })

    it('disables button while retry-all is in progress', async () => {
      mockPost.mockImplementationOnce(() => new Promise(() => {}))
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      await btn.trigger('click')
      await nextTick()
      expect(btn.attributes('disabled')).toBeDefined()
    })

    it('calls retry-all endpoint on click', async () => {
      mockPost.mockResolvedValue({ data: { retried: 1, errors: [], success: true }, error: undefined })
      const wrapper = mountWithItems([failedEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      await btn.trigger('click')
      await flushPromises()
      await nextTick()
      expect(mockPost).toHaveBeenCalledWith(
        '/api/v1/admin/notifications/deliveries/retry-all-failed',
        {},
      )
    })

    it('shows success message after retry-all completes', async () => {
      mockPost.mockResolvedValue({ data: { retried: 2, errors: [], success: true }, error: undefined })
      const wrapper = mountWithItems([failedEntry, deadLetteredEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      await btn.trigger('click')
      await flushPromises()
      await nextTick()
      const msg = wrapper.find('[data-testid="admin-notification-log-retry-success"]')
      expect(msg.exists()).toBe(true)
      expect(msg.text()).toContain('Retried 2 deliveries')
    })

    it('shows partial error message when retry-all has errors', async () => {
      mockPost.mockResolvedValue({ data: { retried: 2, errors: ['timeout'], success: false }, error: undefined })
      const wrapper = mountWithItems([failedEntry, deadLetteredEntry])
      await flushPromises()
      await nextTick()
      const btn = wrapper.find('[data-testid="admin-notification-log-retry-all"]')
      await btn.trigger('click')
      await flushPromises()
      await nextTick()
      const msg = wrapper.find('[data-testid="admin-notification-log-retry-success"]')
      expect(msg.exists()).toBe(true)
      expect(msg.text()).toContain('Retried 2 deliveries with 1 error(s)')
    })
  })
})
