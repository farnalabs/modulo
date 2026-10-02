import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mockAvailableEvents = [
  'hitl_awaiting',
  'run_failed',
  'run_stalled',
  'claim_expired',
  'hitl_overdue',
  'hitl_deadline_warning',
  'budget_exceeded',
  'circuit_breaker_tripped',
  'trigger_deactivated',
  'trigger_streak_alert',
]

function defaultGet(url: string): Promise<{ data: unknown; error: unknown }> {
  if (url === '/api/v1/admin/notifications/available-events') {
    return Promise.resolve({ data: mockAvailableEvents, error: undefined })
  }
  return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => defaultGet(url)),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminNotificationDeliveryLogView from '../views/AdminNotificationDeliveryLogView.vue'
import AppSelect from '../components/shared/AppSelect.vue'
import { api } from '../lib/api/client'

describe('AdminNotificationDeliveryLogView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(
      (url: string) => defaultGet(url),
    )
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

  it('offers every registry event in the event-type filter (FAR-1319)', async () => {
    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()

    // The filter's event list is fetched from the server-side registry, not
    // hardcoded — hitl_deadline_warning and the other later-registered events
    // must be selectable without a frontend edit.
    expect(api.GET).toHaveBeenCalledWith('/api/v1/admin/notifications/available-events')

    // The event-type Select is the second AppSelect in the filter bar.
    const eventSelect = wrapper.findAllComponents(AppSelect).at(1)
    expect(eventSelect).toBeDefined()
    const options = eventSelect!.props('options') as Array<{ value: string; label: string }>
    const values = options.map((o) => o.value)
    expect(values).toContain('__all__')
    expect(values).toContain('hitl_deadline_warning')
    expect(values).toContain('circuit_breaker_tripped')

    // Every registry event renders a human-readable label, never its raw
    // snake_case name: the later-registered events used to fall through to
    // the raw name for want of an en-US key (FAR-1319 review).
    const labelFor = (value: string) => options.find((o) => o.value === value)?.label
    expect(labelFor('run_stalled')).toBe('Run Stalled')
    expect(labelFor('budget_exceeded')).toBe('Budget Exceeded')
    expect(labelFor('circuit_breaker_tripped')).toBe('Circuit Breaker Tripped')
    expect(labelFor('trigger_deactivated')).toBe('Trigger Deactivated')
    expect(labelFor('trigger_streak_alert')).toBe('Trigger Streak Alert')
    expect(labelFor('hitl_deadline_warning')).toBe('HITL Deadline Warning')
  })

  it('falls back to the raw event name when no i18n label exists (FAR-1319)', async () => {
    const mockGet = vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>)
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/admin/notifications/available-events') {
        return Promise.resolve({ data: ['brand_new_event'], error: undefined })
      }
      return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
    })

    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()

    // An event registered on the server before its en-US label ships must
    // still be selectable — it degrades to its raw snake_case name rather than
    // rendering "undefined" or dropping out of the filter.
    const eventSelect = wrapper.findAllComponents(AppSelect).at(1)
    const options = eventSelect!.props('options') as Array<{ value: string; label: string }>
    const option = options.find((o) => o.value === 'brand_new_event')
    expect(option).toBeDefined()
    expect(option!.label).toBe('brand_new_event')
  })

  it('surfaces an inline retry when the registry fetch fails (FAR-1319)', async () => {
    const mockGet = vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>)
    let registryFails = true
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/admin/notifications/available-events') {
        return registryFails
          ? Promise.resolve({ data: undefined, error: { detail: 'boom' } })
          : Promise.resolve({ data: mockAvailableEvents, error: undefined })
      }
      return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
    })

    const wrapper = mount(AdminNotificationDeliveryLogView)
    await flushPromises()
    await nextTick()

    // A failed registry fetch degrades the filter to "All types" but is no
    // longer silent: a small inline retry mirrors the team picker.
    const msg = wrapper.find('[data-testid="admin-notification-log-events-unavailable"]')
    expect(msg.exists()).toBe(true)
    const retry = wrapper.find('[data-testid="admin-notification-log-events-retry"]')
    expect(retry.exists()).toBe(true)

    registryFails = false
    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="admin-notification-log-events-unavailable"]').exists()).toBe(false)
  })
})
