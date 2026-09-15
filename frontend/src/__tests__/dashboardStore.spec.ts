import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

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
    setActivePinia(createPinia())
    vi.clearAllMocks()
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
