import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { clearAllRegistrations, getHandlers } from '../stores/syncRegistry'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))

import { useDashboardStore, type DashboardSummary } from '../stores/dashboard'

const period = {
  days: 7,
  metrics: {
    total_runs: { current: 50, previous: 40, delta_pct: 25.0 },
    active_pipelines: { current: 8, previous: 9, delta_pct: -11.1 },
    run_counts_by_status: {
      running: { current: 0, previous: 0, delta_pct: null },
      awaiting_human: { current: 0, previous: 0, delta_pct: null },
      failed: { current: 5, previous: 3, delta_pct: 66.7 },
      idle: { current: 0, previous: 0, delta_pct: null },
    },
    eval_pass_rate: { current: 82.5, previous: 80.0, delta_pct: 3.1 },
    spend: { current: 100.25, previous: 90.0, delta_pct: 11.4 },
    tokens: { current: 15000, previous: 12000, delta_pct: 25.0 },
    success_rate: { current: 85.0, previous: 80.0, delta_pct: 6.2 },
    avg_duration_ms: { current: 1250.5, previous: 1300.0, delta_pct: -3.8 },
  },
}

const baseSummary: DashboardSummary = {
  total_runs: 142,
  active_pipelines: 8,
  run_counts_by_status: { running: 3, awaiting_human: 2, failed: 5, idle: 12 },
  teams: [],
  eval_pass_rate: {
    overall_pass_rate: 82.5,
    total_evals: 70,
    passed_evals: 56,
    per_pipeline: {},
    per_team_pipeline: {},
  },
  trend: [],
  recent_runs: [],
  config_warnings: [],
}

describe('useDashboardStore', () => {
  // The store's setup calls useI18n(), which requires a component context —
  // mount a host component so the store is instantiated inside a Vue setup.
  function createStoreHost() {
    let store: ReturnType<typeof useDashboardStore> | null = null
    const Host = defineComponent({
      setup() {
        store = useDashboardStore()
        return () => null
      },
    })
    return { Host, getStore: () => store! }
  }

  beforeEach(() => {
    clearAllRegistrations()
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetchSummary loads all-time data and computes trend spend', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary, trend: [{ date: '2026-09-01', run_count: 1, eval_pass_rate: null, token_spend_usd: 12.5, avg_time_to_approve_ms: null, rejection_rate: null }] }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    await store.fetchSummary()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/dashboard/summary', { params: { query: {} } })
    expect(store.summary?.total_runs).toBe(142)
    expect(store.totalSpend).toBe(12.5)
    expect(store.loading).toBe(false)
    expect(store.summaryError).toBeNull()
  })

  it('fetchSummary sends the selected period and reports invalid payloads', async () => {
    mockGet.mockResolvedValue({ data: { total_runs: 1 }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    await store.fetchSummary(30)

    expect(mockGet).toHaveBeenCalledWith('/api/v1/dashboard/summary', { params: { query: { days: 30 } } })
    expect(store.summary).toBeNull()
    expect(store.summaryError).toBe('Received invalid dashboard data from server.')
    expect(store.loading).toBe(false)
  })

  it('fetchSummary reports API errors and ignores overlapping requests', async () => {
    let resolveRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementation(() => new Promise((resolve) => { resolveRequest = resolve }))
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    const first = store.fetchSummary(7)
    await Promise.resolve()
    const second = store.fetchSummary(14)
    resolveRequest?.({ data: undefined, error: { detail: 'summary unavailable' } })

    await first
    await second

    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(store.summaryError).toBe('summary unavailable')
    expect(store.loading).toBe(false)
  })

  it('fetchPeriodMetrics merges only the period block without touching the rest of the summary', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary, period }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    store.summary = { ...baseSummary }
    await store.fetchPeriodMetrics(7)
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 7 } } }),
    )
    expect(store.summary?.period).toEqual(period)
    expect(store.summary?.total_runs).toBe(142)
    expect(store.summary?.trend).toEqual([])
  })

  it('discards stale period responses when a newer window request completes first', async () => {
    const requests = new Map<number, { resolve: (value: unknown) => void }>()
    mockGet.mockImplementation((url: string, options: { params: { query: { days: number } } }) => {
      if (url !== '/api/v1/dashboard/summary') return Promise.resolve({ data: null, error: undefined })
      const days = options.params.query.days
      return new Promise((resolve) => requests.set(days, { resolve }))
    })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    store.summary = { ...baseSummary }

    const first = store.fetchPeriodMetrics(7)
    const second = store.fetchPeriodMetrics(30)
    requests.get(30)?.resolve({ data: { ...baseSummary, period: { ...period, days: 30 } }, error: undefined })
    await second
    requests.get(7)?.resolve({ data: { ...baseSummary, period }, error: undefined })
    await first

    expect(store.summary?.period?.days).toBe(30)
    expect(store.periodRefreshing).toBe(false)
  })

  it('fetchPeriodMetrics does NOT set the global loading flag (no full-page flash)', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary, period }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    store.summary = { ...baseSummary }
    store.loading = true
    await store.fetchPeriodMetrics(7)
    expect(store.loading).toBe(true)
    expect(store.periodRefreshing).toBe(false)
  })

  it('fetchPeriodMetrics populates a null summary on first use', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary, period }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    await store.fetchPeriodMetrics(7)
    expect(store.summary?.period).toEqual(period)
    expect(store.summary?.total_runs).toBe(142)
  })

  it('fetchPeriodMetrics sets an error without wiping the summary on failure', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: { detail: 'boom' } })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    store.summary = { ...baseSummary }
    await store.fetchPeriodMetrics(7)
    expect(store.summaryError).toBeTruthy()
    expect(store.summary?.total_runs).toBe(142)
    expect(store.periodRefreshing).toBe(false)
    await flushPromises()
  })

  it('fetchSummary formats thrown request errors and clears loading state', async () => {
    mockGet.mockRejectedValueOnce(new Error('summary network down'))
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    await store.fetchSummary(7)

    expect(store.summaryError).toBe('summary network down')
    expect(store.loading).toBe(false)
  })

  it('fetchPeriodMetrics reports thrown request errors without wiping the summary', async () => {
    mockGet.mockRejectedValueOnce(new Error('period network down'))
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    store.summary = { ...baseSummary }

    await store.fetchPeriodMetrics(7)

    expect(store.summary?.total_runs).toBe(142)
    expect(store.summaryError).toBe('period network down')
    expect(store.periodRefreshing).toBe(false)
  })

  it('fetchPeriodMetrics reports invalid data and preserves the selected summary', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary, recent_runs: 'invalid', period: { days: 7 } }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    store.summary = { ...baseSummary }

    await store.fetchPeriodMetrics(7)

    expect(store.summary?.period).toBeUndefined()
    expect(store.summaryError).toBe('Received invalid dashboard data from server.')
    expect(store.periodRefreshing).toBe(false)
  })

  it('fetchTrends validates days before making an API request', async () => {
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    await store.fetchTrends(0)

    expect(mockGet).not.toHaveBeenCalled()
    expect(store.trendsError).toBe('Invalid days parameter: must be a positive integer.')
    expect(store.trendsLoading).toBe(false)
  })

  it('fetchTrends reports API and thrown errors and clears loading state', async () => {
    mockGet.mockResolvedValueOnce({ data: undefined, error: { detail: 'trend unavailable' } })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    await store.fetchTrends(7)
    expect(store.trendsError).toBe('trend unavailable')
    expect(store.trendsLoading).toBe(false)

    mockGet.mockRejectedValueOnce(new Error('network down'))
    await store.fetchTrends(7)
    expect(store.trendsError).toBe('network down')
    expect(store.trendsLoading).toBe(false)
  })

  it('refreshes once for run and pipeline sync events and disposes handlers', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    await store.fetchSummary(7)
    const callsBefore = mockGet.mock.calls.length

    const event = { type: 'run' as const, id: 'run-1', action: 'updated' as const, version: 1, org_id: 'org-1' }
    const runHandlers = getHandlers('run')
    const pipelineHandlers = getHandlers('pipeline')
    for (const handler of runHandlers) handler(event)
    for (const handler of runHandlers) handler(event)
    for (const handler of pipelineHandlers) handler({ ...event, type: 'pipeline', id: 'pipeline-1' })
    await vi.waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThan(callsBefore))
    await vi.waitFor(() => expect(store.loading).toBe(false))
    expect(mockGet.mock.calls.length).toBe(callsBefore + 1)
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/dashboard/summary', { params: { query: { days: 7 } } })

    store.disposeHandlers()
    expect(getHandlers('run').size).toBe(0)
    expect(getHandlers('pipeline').size).toBe(0)
  })

  it('uses period spend instead of summing the trend when available', async () => {
    mockGet.mockResolvedValue({ data: { ...baseSummary, trend: [{ date: '2026-09-01', run_count: 1, eval_pass_rate: null, token_spend_usd: 12.5, avg_time_to_approve_ms: null, rejection_rate: null }], period }, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    await store.fetchSummary(7)

    expect(store.totalSpend).toBe(100.25)
  })

  it('fetchTrends stores hitl_volume and rejection_trend from the response', async () => {
    const trendsResponse = {
      days: 7,
      run_counts: [{ date: '2026-09-01', run_count: 10 }],
      eval_pass_rates: [{ date: '2026-09-01', total_evals: 5, passed_evals: 4, pass_rate: 80 }],
      token_spend: [{ date: '2026-09-01', total_spend_usd: 1.5 }],
      hitl_volume: [{ date: '2026-09-01', total_decisions: 3, approved_count: 2, rejected_count: 1, rejection_rate: 33.3, avg_time_to_approve_ms: 45000 }],
      rejection_trend: [{ date: '2026-09-01', rolling_rejection_rate: 25, raw_rejection_rate: 33.3 }],
      correlation: [],
      feedback_volume: [],
    }
    mockGet.mockResolvedValue({ data: trendsResponse, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    await store.fetchTrends(7)
    expect(store.trends).toBeTruthy()
    expect(store.trends?.hitl_volume).toHaveLength(1)
    expect(store.trends?.hitl_volume[0].avg_time_to_approve_ms).toBe(45000)
    expect(store.trends?.rejection_trend).toHaveLength(1)
    expect(store.trends?.rejection_trend[0].rolling_rejection_rate).toBe(25)
  })

  it('discards stale trend responses when a newer window request completes first', async () => {
    const requests = new Map<number, { resolve: (value: unknown) => void }>()
    mockGet.mockImplementation((url: string, options: { params: { query: { days: number } } }) => {
      if (url !== '/api/v1/dashboard/trends') return Promise.resolve({ data: null, error: undefined })
      const days = options.params.query.days
      return new Promise((resolve) => requests.set(days, { resolve }))
    })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()

    const first = store.fetchTrends(7)
    const second = store.fetchTrends(30)
    requests.get(30)?.resolve({
      data: {
        days: 30,
        run_counts: [{ date: '2026-09-30', run_count: 30 }],
        eval_pass_rates: [],
        token_spend: [],
        hitl_volume: [],
        rejection_trend: [],
        correlation: [],
        feedback_volume: [],
      },
      error: undefined,
    })
    await second
    requests.get(7)?.resolve({
      data: {
        days: 7,
        run_counts: [{ date: '2026-09-07', run_count: 7 }],
        eval_pass_rates: [],
        token_spend: [],
        hitl_volume: [],
        rejection_trend: [],
        correlation: [],
        feedback_volume: [],
      },
      error: undefined,
    })
    await first

    expect(store.trends?.days).toBe(30)
    expect(store.trends?.run_counts[0].run_count).toBe(30)
    expect(store.trendsLoading).toBe(false)
  })

  it('fetchTrends rejects responses missing hitl_volume or rejection_trend', async () => {
    const incompleteResponse = {
      days: 7,
      run_counts: [{ date: '2026-09-01', run_count: 10 }],
      eval_pass_rates: [],
      token_spend: [],
      // hitl_volume and rejection_trend missing
      correlation: [],
      feedback_volume: [],
    }
    mockGet.mockResolvedValue({ data: incompleteResponse, error: undefined })
    const { Host, getStore } = createStoreHost()
    mount(Host)
    const store = getStore()
    await store.fetchTrends(7)
    expect(store.trends).toBeNull()
    expect(store.trendsError).toBeTruthy()
  })
})
