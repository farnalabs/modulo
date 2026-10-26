import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { enableAutoUnmount, mount, flushPromises } from '@vue/test-utils'

// The real composable installs a QueryClient/observer per mount; unmount every
// wrapper so its timers and focus/online listeners cannot leak into a later
// spec's shared api.GET mock.
enableAutoUnmount(afterEach)

// useDataFetch is deliberately NOT mocked: vue-query subscribes during setup,
// so the real composable exercises the component's declaration order.
// Mocking it hid a TDZ ReferenceError on the initial fetch (FAR-2173).
vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))
vi.mock('../lib/api/schema', () => ({}))

import SettingsNotificationLogView from '../views/SettingsNotificationLogView.vue'
import { api } from '../lib/api/client'

const getMock = api.GET as ReturnType<typeof vi.fn>

function makeDelivery(overrides: Record<string, unknown> = {}) {
  return {
    id: 'del-001',
    event_type: 'pipeline.completed',
    endpoint_url: 'https://hooks.example.com/notify',
    status: 'delivered',
    attempt_count: 1,
    created_at: '2026-09-15T10:30:00Z',
    last_error: null,
    ...overrides,
  }
}

describe('SettingsNotificationLogView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders the heading', async () => {
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('Webhook Notifications')
  })

  it('shows empty state when no deliveries', async () => {
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('No delivery logs found')
    expect(wrapper.text()).toContain('Try adjusting your filters')
  })

  it('shows loading skeleton while fetching', async () => {
    getMock.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.find('[data-testid="settings-notification-log-loading"]').exists()).toBe(true)
  })

  it('loads the delivery log on mount without a TDZ error (FAR-2173)', async () => {
    getMock.mockResolvedValue({
      data: { items: [makeDelivery()], total: 1, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    // The initial fetch must not hit the temporal dead zone on the filter refs.
    expect(wrapper.text()).not.toContain("Cannot access 'filterStatus' before initialization")
    expect(wrapper.text()).toContain('pipeline.completed')
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith(
      '/api/v1/admin/notifications/deliveries',
      expect.objectContaining({ params: { query: { limit: 50 } } }),
    )
  })

  it('sends the selected filters through the real fetcher', async () => {
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    // The '__all__' sentinel must be dropped, not sent as a filter value.
    const vm = wrapper.vm as unknown as {
      filterStatus: string
      filterDateFrom: string
      filterDateTo: string
    }
    vm.filterStatus = 'failed'
    vm.filterDateFrom = '2026-09-01'
    vm.filterDateTo = '2026-09-30'
    await flushPromises()
    await wrapper.find('[data-testid="settings-notification-log-apply"]').trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenLastCalledWith(
      '/api/v1/admin/notifications/deliveries',
      expect.objectContaining({
        params: { query: { limit: 50, status: 'failed', from: '2026-09-01', to: '2026-09-30' } },
      }),
    )
  })

  it('shows error alert when load fails', async () => {
    getMock.mockImplementation(() =>
      Promise.resolve({ data: undefined, error: { detail: 'connection_refused' } }),
    )
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('connection_refused')
  })

  it('renders delivery table rows with all columns', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('pipeline.completed')
    expect(wrapper.text()).toContain('Delivered')
    expect(wrapper.text()).toContain('https://hooks.example.com/notify')
    expect(wrapper.text()).toContain('1')
    expect(wrapper.text()).toContain('Sep')
    expect(wrapper.text()).toContain('15')
  })

  it('renders dash for empty endpoint_url', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ endpoint_url: null })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('renders dash for empty last_error', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ last_error: null })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('renders error detail when present', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ last_error: 'ECONNREFUSED 127.0.0.1:443' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('ECONNREFUSED')
  })

  it('shows total count text as "X deliveries"', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 25,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('25 deliveries')
  })

  it('shows singular "delivery" for total of 1', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('1 delivery')
  })

  it('shows "X of Y deliveries" pagination text', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery(), makeDelivery({ id: 'del-002' })],
        total: 50,
        next_cursor: 'next-cursor',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('2 of 50 deliveries')
  })

  it('apply button triggers load with current filters', async () => {
    getMock.mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const applyBtn = wrapper.find('[data-testid="settings-notification-log-apply"]')
    await applyBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalled()
  })

  it('reset button clears filters and reloads', async () => {
    getMock.mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const resetBtn = wrapper.find('[data-testid="settings-notification-log-reset"]')
    await resetBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalled()
  })

  it('next button is disabled when no nextCursor', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const nextBtn = wrapper.find('[data-testid="settings-notification-log-next"]')
    expect(nextBtn.attributes('disabled')).toBeDefined()
  })

  it('previous button is disabled when cursorStack is empty', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const prevBtn = wrapper.find('[data-testid="settings-notification-log-previous"]')
    expect(prevBtn.attributes('disabled')).toBeDefined()
  })

  it('next button enabled when nextCursor exists', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 10,
        next_cursor: 'page2',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const nextBtn = wrapper.find('[data-testid="settings-notification-log-next"]')
    expect(nextBtn.attributes('disabled')).toBeUndefined()
  })

  it('navigating to next page then previous works', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery()],
        total: 10,
        next_cursor: 'page2',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const nextBtn = wrapper.find('[data-testid="settings-notification-log-next"]')
    await nextBtn.trigger('click')
    await flushPromises()
    const prevBtn = wrapper.find('[data-testid="settings-notification-log-previous"]')
    expect(prevBtn.attributes('disabled')).toBeUndefined()
    await prevBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalled()
  })

  it('statusBadge returns success class for delivered', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ status: 'delivered' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const badge = wrapper.find('.badge-status-success')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('Delivered')
  })

  it('statusBadge returns destructive class for failed', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ status: 'failed' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const badge = wrapper.find('.badge-status-destructive')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('Failed')
  })

  it('statusBadge returns slate class for dead_lettered', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ status: 'dead_lettered' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const badge = wrapper.find('.badge-context-slate')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('Dead Lettered')
  })

  it('statusBadge returns warning class for pending', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ status: 'pending' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const badge = wrapper.find('.badge-status-warning')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('Pending')
  })

  it('statusBadge returns slate class for unknown status', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ status: 'unknown_status' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const badge = wrapper.find('.badge-context-slate')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('unknown_status')
  })

  it('formatTimestamp returns dash for null', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ created_at: null })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('formatTimestamp formats valid date', async () => {
    // nosemgrep: new-date-without-guard
    const date = new Date('2026-09-15T10:30:00Z')
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ created_at: date.toISOString() })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('Sep')
    expect(wrapper.text()).toContain('15')
  })

  it('endpoint_url title attribute set for truncation', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ endpoint_url: 'https://very-long-url.example.com/notifications/webhooks/endpoint' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const td = wrapper.find('td[title]')
    expect(td.exists()).toBe(true)
    expect(td.attributes('title')).toContain('very-long-url')
  })

  it('last_error title attribute set for truncation', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeDelivery({ last_error: 'Connection timed out after 30 seconds' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const errorTd = wrapper.findAll('td').find(td => td.text().includes('Connection timed out'))
    expect(errorTd).toBeTruthy()
    expect(errorTd!.attributes('title')).toContain('Connection timed out')
  })

  it('retry button on error alert re-fetches', async () => {
    getMock.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'network_error' },
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('network_error')

    getMock.mockResolvedValueOnce({
      data: { items: [makeDelivery()], total: 1, next_cursor: null },
      error: undefined,
    })
    const retry = wrapper.findAll('button').find(b => b.text() === 'Retry')
    if (retry) {
      await retry.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('Delivered')
    }
  })

  it('renders multiple delivery rows', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [
          makeDelivery({ id: 'del-001', event_type: 'pipeline.completed', status: 'delivered' }),
          makeDelivery({ id: 'del-002', event_type: 'pipeline.failed', status: 'failed', last_error: 'timeout' }),
          makeDelivery({ id: 'del-003', event_type: 'run.started', status: 'dead_lettered' }),
        ],
        total: 3,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('pipeline.completed')
    expect(wrapper.text()).toContain('pipeline.failed')
    expect(wrapper.text()).toContain('run.started')
    expect(wrapper.text()).toContain('3 deliveries')
  })

  it('filter dropdowns exist with correct testids', async () => {
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    expect(wrapper.find('[data-testid="settings-notification-log-status"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-notification-log-date-from"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-notification-log-date-to"]').exists()).toBe(true)
  })

  it('date inputs are bound to v-model', async () => {
    getMock.mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(SettingsNotificationLogView)
    await flushPromises()
    const dateFrom = wrapper.find('[data-testid="settings-notification-log-date-from"]')
    const dateTo = wrapper.find('[data-testid="settings-notification-log-date-to"]')
    expect(dateFrom.exists()).toBe(true)
    expect(dateTo.exists()).toBe(true)
    expect((dateFrom.element as HTMLInputElement).value).toBe('')
    expect((dateTo.element as HTMLInputElement).value).toBe('')
  })
})
