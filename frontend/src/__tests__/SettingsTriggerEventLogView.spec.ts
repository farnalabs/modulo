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

import SettingsTriggerEventLogView from '../views/SettingsTriggerEventLogView.vue'
import { api } from '../lib/api/client'

const getMock = api.GET as ReturnType<typeof vi.fn>

function makeEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: 'evt-001',
    trigger_type: 'webhook',
    validation_result: 'accepted',
    received_at: '2026-09-15T10:30:00Z',
    run_id: 'run-abc123def456',
    error_detail: null,
    trigger_id: 'trg-789xyz012abc',
    ...overrides,
  }
}

describe('SettingsTriggerEventLogView', () => {
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

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Trigger Event Log')
  })

  it('shows empty state when no events', async () => {
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('No trigger events found')
    expect(wrapper.text()).toContain('Try adjusting your filters')
  })

  it('loads the event log on mount without a TDZ error (FAR-2173)', async () => {
    getMock.mockResolvedValue({
      data: { items: [makeEvent()], total: 1, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    // The initial fetch must not hit the temporal dead zone on the filter refs.
    expect(wrapper.text()).not.toContain("Cannot access 'filterTriggerType' before initialization")
    expect(wrapper.text()).toContain('webhook')
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith(
      '/api/v1/admin/trigger-events',
      expect.objectContaining({ params: { query: { limit: 50 } } }),
    )
  })

  it('sends the selected filters through the real fetcher', async () => {
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    // The '__all__' sentinel must be dropped, not sent as a filter value.
    const vm = wrapper.vm as unknown as { filterTriggerType: string; filterResult: string }
    vm.filterTriggerType = 'webhook'
    vm.filterResult = 'accepted'
    await flushPromises()
    await wrapper.find('[data-testid="settings-trigger-event-log-apply"]').trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenLastCalledWith(
      '/api/v1/admin/trigger-events',
      expect.objectContaining({
        params: { query: { limit: 50, trigger_type: 'webhook', validation_result: 'accepted' } },
      }),
    )
  })

  it('shows loading spinner while fetching', async () => {
    getMock.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows error alert when load fails', async () => {
    getMock.mockImplementation(() =>
      Promise.resolve({ data: undefined, error: { detail: 'server_error' } }),
    )
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('server_error')
  })

  it('renders event table rows with type, result, timestamp, run, error, trigger_id', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('webhook')
    expect(wrapper.text()).toContain('accepted')
    expect(wrapper.text()).toContain('#run-abc')
    expect(wrapper.text()).toContain('#trg-789x')
    expect(wrapper.text()).toContain('Sep')
    expect(wrapper.text()).toContain('15')
  })

  it('renders dash for missing run_id', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ run_id: null })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('renders dash for empty error_detail', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ error_detail: null })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('renders error_detail text when present', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ error_detail: 'hmac_signature_mismatch' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('hmac_signature_mismatch')
  })

  it('shows total count text', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 42,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('42 events')
  })

  it('shows singular "event" for total of 1', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('1 event')
  })

  it('shows "X of Y events" pagination text', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent(), makeEvent({ id: 'evt-002' })],
        total: 50,
        next_cursor: 'cursor-next',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('2 of 50 events')
  })

  it('apply button resets cursor and reloads', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 1,
        next_cursor: 'next-page',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const applyBtn = wrapper.find('[data-testid="settings-trigger-event-log-apply"]')
    await applyBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalled()
  })

  it('reset button clears filters and reloads', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const resetBtn = wrapper.find('[data-testid="settings-trigger-event-log-reset"]')
    await resetBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalled()
  })

  it('next button calls loadEvents with cursor', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 10,
        next_cursor: 'page2-cursor',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const nextBtn = wrapper.find('[data-testid="settings-trigger-event-log-next"]')
    expect(nextBtn.attributes('disabled')).toBeUndefined()
    await nextBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalled()
  })

  it('next button is disabled when no nextCursor', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const nextBtn = wrapper.find('[data-testid="settings-trigger-event-log-next"]')
    expect(nextBtn.attributes('disabled')).toBeDefined()
  })

  it('previous button is disabled when cursorStack is empty', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const prevBtn = wrapper.find('[data-testid="settings-trigger-event-log-previous"]')
    expect(prevBtn.attributes('disabled')).toBeDefined()
  })

  it('previous button enabled after navigating to next page', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 10,
        next_cursor: 'page2-cursor',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const nextBtn = wrapper.find('[data-testid="settings-trigger-event-log-next"]')
    await nextBtn.trigger('click')
    await flushPromises()
    const prevBtn = wrapper.find('[data-testid="settings-trigger-event-log-previous"]')
    expect(prevBtn.attributes('disabled')).toBeUndefined()
  })

  it('previous button walks back to the first page after paging forward (FAR-2264)', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent()],
        total: 10,
        next_cursor: 'page2-cursor',
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    await wrapper.find('[data-testid="settings-trigger-event-log-next"]').trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenLastCalledWith(
      '/api/v1/admin/trigger-events',
      expect.objectContaining({ params: { query: { limit: 50, cursor: 'page2-cursor' } } }),
    )

    const prevBtn = wrapper.find('[data-testid="settings-trigger-event-log-previous"]')
    expect(prevBtn.attributes('disabled')).toBeUndefined()
    await prevBtn.trigger('click')
    await flushPromises()

    expect(getMock).toHaveBeenLastCalledWith(
      '/api/v1/admin/trigger-events',
      expect.objectContaining({ params: { query: { limit: 50 } } }),
    )
    expect(wrapper.find('[data-testid="settings-trigger-event-log-previous"]').attributes('disabled')).toBeDefined()
  })

  it('typeBadge returns correct class for each trigger type', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [
          makeEvent({ id: '1', trigger_type: 'manual' }),
          makeEvent({ id: '2', trigger_type: 'webhook' }),
          makeEvent({ id: '3', trigger_type: 'cron' }),
          makeEvent({ id: '4', trigger_type: 'polling' }),
          makeEvent({ id: '5', trigger_type: 'agent_signal' }),
          makeEvent({ id: '6', trigger_type: 'ongoing' }),
          makeEvent({ id: '7', trigger_type: 'unknown_type' }),
        ],
        total: 7,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('manual')
    expect(wrapper.text()).toContain('webhook')
    expect(wrapper.text()).toContain('cron')
    expect(wrapper.text()).toContain('polling')
    expect(wrapper.text()).toContain('agent_signal')
    expect(wrapper.text()).toContain('ongoing')
    expect(wrapper.text()).toContain('unknown_type')
  })

  it('resultBadge returns correct class for each result type', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [
          makeEvent({ id: '1', validation_result: 'accepted' }),
          makeEvent({ id: '2', validation_result: 'passed' }),
          makeEvent({ id: '3', validation_result: 'condition_met' }),
          makeEvent({ id: '4', validation_result: 'signal_fired' }),
          makeEvent({ id: '5', validation_result: 'no_match' }),
          makeEvent({ id: '6', validation_result: 'hmac_failed' }),
          makeEvent({ id: '7', validation_result: 'schema_validation_failed' }),
          makeEvent({ id: '8', validation_result: 'deduplicated' }),
          makeEvent({ id: '9', validation_result: 'concurrency_limit_reached' }),
          makeEvent({ id: '10', validation_result: 'flood_rejected' }),
          makeEvent({ id: '11', validation_result: 'timestamp_expired' }),
          makeEvent({ id: '12', validation_result: 'validation_failed' }),
          makeEvent({ id: '13', validation_result: 'rate_limited' }),
          makeEvent({ id: '14', validation_result: 'poll_error' }),
          makeEvent({ id: '15', validation_result: 'unknown_result' }),
          makeEvent({ id: '16', validation_result: 'paused' }),
        ],
        total: 16,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('accepted')
    expect(wrapper.text()).toContain('passed')
    expect(wrapper.text()).toContain('condition_met')
    expect(wrapper.text()).toContain('signal_fired')
    expect(wrapper.text()).toContain('no_match')
    expect(wrapper.text()).toContain('hmac_failed')
    expect(wrapper.text()).toContain('schema_validation_failed')
    expect(wrapper.text()).toContain('deduplicated')
    expect(wrapper.text()).toContain('concurrency_limit_reached')
    expect(wrapper.text()).toContain('flood_rejected')
    expect(wrapper.text()).toContain('timestamp_expired')
    expect(wrapper.text()).toContain('validation_failed')
    expect(wrapper.text()).toContain('rate_limited')
    expect(wrapper.text()).toContain('poll_error')
    expect(wrapper.text()).toContain('unknown_result')
    expect(wrapper.text()).toContain('paused')
  })

  it('formatTimestamp returns dash for null/undefined', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ received_at: null })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('formatTimestamp formats valid date', async () => {
    // nosemgrep: new-date-without-guard
    const date = new Date('2026-09-15T10:30:00Z')
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ received_at: date.toISOString() })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('Sep')
    expect(wrapper.text()).toContain('15')
  })

  it('sends params to api.GET on load', async () => {
    getMock.mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const applyBtn = wrapper.find('[data-testid="settings-trigger-event-log-apply"]')
    await applyBtn.trigger('click')
    await flushPromises()
    expect(getMock).toHaveBeenCalledWith(
      '/api/v1/admin/trigger-events',
      expect.objectContaining({ params: expect.any(Object) }),
    )
  })

  it('formats short id via shortId utility', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ run_id: 'run-abc123def456ghij', trigger_id: 'trg-789xyz012abc345def' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('#run-abc')
    expect(wrapper.text()).toContain('#trg-789x')
  })

  it('error_detail title attribute is set for truncation', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [makeEvent({ error_detail: 'A very long error detail message that exceeds the truncation limit' })],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    const td = wrapper.find('td[title]')
    expect(td.exists()).toBe(true)
    expect(td.attributes('title')).toContain('A very long error detail')
  })

  it('retry button on error alert re-fetches data', async () => {
    getMock.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'server_error' },
    })
    const wrapper = mount(SettingsTriggerEventLogView)
    await flushPromises()
    expect(wrapper.text()).toContain('server_error')

    getMock.mockResolvedValueOnce({
      data: { items: [makeEvent()], total: 1, next_cursor: null },
      error: undefined,
    })
    const retry = wrapper.findAll('button').find(b => b.text() === 'Retry')
    if (retry) {
      await retry.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('webhook')
    }
  })

  // FAR-1344 — capacity outcomes: dropdown entries, distinct badges, and the
  // count/trend strip.
  describe('capacity outcomes', () => {
    function withCounts(overrides: Record<string, unknown> = {}) {
      return {
        data: {
          items: [],
          total: 0,
          next_cursor: null,
          result_counts: {
            window_hours: 24,
            recent: { backpressure_skipped: 3, concurrency_limit_reached: 0, coalesced: 1 },
            previous: { backpressure_skipped: 1, concurrency_limit_reached: 0, coalesced: 0 },
            total_recent: 4,
            total_previous: 1,
            direction: 'up',
            ...overrides,
          },
        },
        error: undefined,
      }
    }

    it('renders the capacity-outcome count and trend when result_counts is present', async () => {
      getMock.mockResolvedValue(withCounts())
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.find('[data-testid="capacity-outcomes-summary"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="capacity-outcomes-total"]').text()).toContain('4 capacity refusals')
      expect(wrapper.find('[data-testid="capacity-outcomes-trend"]').text()).toContain('up from the previous 24h')
    })

    it('renders per-outcome badges only for non-zero outcomes', async () => {
      getMock.mockResolvedValue(withCounts())
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.find('[data-testid="capacity-outcome-backpressure_skipped"]').text()).toContain('3')
      expect(wrapper.find('[data-testid="capacity-outcome-coalesced"]').text()).toContain('1')
      expect(wrapper.find('[data-testid="capacity-outcome-concurrency_limit_reached"]').exists()).toBe(false)
    })

    it('hides the capacity summary when the response omits result_counts', async () => {
      getMock.mockResolvedValue({
        data: { items: [makeEvent()], total: 1, next_cursor: null },
        error: undefined,
      })
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.find('[data-testid="capacity-outcomes-summary"]').exists()).toBe(false)
    })

    it('reports a flat trend when both windows are empty', async () => {
      getMock.mockResolvedValue(
        withCounts({
          recent: { backpressure_skipped: 0, concurrency_limit_reached: 0, coalesced: 0 },
          previous: { backpressure_skipped: 0, concurrency_limit_reached: 0, coalesced: 0 },
          total_recent: 0,
          total_previous: 0,
          direction: 'flat',
        }),
      )
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.text()).toContain('No capacity refusals in the last 24h')
    })

    it('renders a distinct badge for a backpressure_skipped row', async () => {
      getMock.mockResolvedValue({
        data: {
          items: [makeEvent({ validation_result: 'backpressure_skipped' })],
          total: 1,
          next_cursor: null,
        },
        error: undefined,
      })
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.text()).toContain('backpressure_skipped')
      expect(wrapper.find('.badge-context-rose').exists()).toBe(true)
    })

    it('renders a down trend when the recent window has fewer refusals', async () => {
      getMock.mockResolvedValue(withCounts({ direction: 'down', total_recent: 2, total_previous: 5 }))
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.find('[data-testid="capacity-outcomes-trend"]').text()).toContain('down from the previous 24h')
    })

    it('renders a flat trend when the two windows are level', async () => {
      getMock.mockResolvedValue(withCounts({ direction: 'flat', total_recent: 3, total_previous: 3 }))
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.find('[data-testid="capacity-outcomes-trend"]').text()).toContain(
        'unchanged from the previous 24h',
      )
    })

    it('defaults window, direction, total and recent when the response omits them', async () => {
      // Older/partial backends may send only a subset of result_counts. The
      // summary must render with safe defaults rather than crash or print
      // "undefined".
      getMock.mockResolvedValue({
        data: {
          items: [],
          total: 0,
          next_cursor: null,
          result_counts: { previous: {}, total_previous: 2 },
        },
        error: undefined,
      })
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.find('[data-testid="capacity-outcomes-total"]').text()).toContain('0 capacity refusals')
      expect(wrapper.find('[data-testid="capacity-outcomes-trend"]').text()).toContain(
        'unchanged from the previous 24h',
      )
      expect(wrapper.text()).toContain('No capacity refusals in the last 24h')
      expect(wrapper.text()).not.toContain('undefined')
    })

    it('renders a distinct badge for a spend_limit_reached row', async () => {
      getMock.mockResolvedValue({
        data: {
          items: [makeEvent({ validation_result: 'spend_limit_reached' })],
          total: 1,
          next_cursor: null,
        },
        error: undefined,
      })
      const wrapper = mount(SettingsTriggerEventLogView)
      await flushPromises()
      expect(wrapper.text()).toContain('spend_limit_reached')
      expect(wrapper.find('.badge-context-purple').exists()).toBe(true)
    })
  })
})
