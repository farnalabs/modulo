import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))
vi.mock('vue-echarts', () => ({
  default: { name: 'VChart', props: ['option'], template: '<div class="vchart-stub" />' },
}))
vi.mock('echarts', () => ({ default: {} }))

import AnalyticsView from '../views/AnalyticsView.vue'
import {
  useAnalyticsStore,
  serializeFilters,
  buildChartOption,
  computeTrendDelta,
  formatDeltaPercent,
  formatMeasureValue,
  aggregateByKey,
  previousWindowParams,
  type AnalyticsBucket,
} from '../stores/analytics'

// Fixed UTC instant for deterministic assertions — the ISO literal is always valid.
const FIXED_NOW = new Date('2026-08-06T12:00:00Z') // nosemgrep: new-date-without-guard

// UTC day-string helpers mirroring the store's day-window logic. Deriving the
// expected dates from FIXED_NOW (instead of hardcoded literals) keeps the
// assertions timezone-robust and correct if FIXED_NOW is ever changed.
const DAY_MS = 86400000
const HOUR_MS = 3600000
function utcDay(d: Date): string {
  return d.toISOString().slice(0, 10)
}
function daysBefore(d: Date, days: number): string {
  return utcDay(new Date(d.getTime() - days * DAY_MS)) // nosemgrep: new-date-without-guard
}
function hoursBefore(d: Date, hours: number): string {
  return new Date(d.getTime() - hours * HOUR_MS).toISOString() // nosemgrep: new-date-without-guard
}

const validResponse = {
  group_by: 'day',
  dimension: null,
  date_from: '2026-07-30',
  date_to: '2026-08-06',
  buckets: [
    {
      date: '2026-08-01',
      count: 3,
      total_cost_usd: 1.5,
      total_tokens: 1200,
      avg_duration_ms: 2500,
      success_rate: 0.667,
    },
    {
      date: '2026-08-02',
      count: 5,
      total_cost_usd: 2.5,
      total_tokens: 2100,
      avg_duration_ms: 3000,
      success_rate: 0.8,
    },
  ],
}

const emptyResponse = {
  group_by: 'day',
  dimension: null,
  date_from: '2026-07-30',
  date_to: '2026-08-06',
  buckets: [
    { date: '2026-07-30', count: 0 },
    { date: '2026-07-31', count: 0 },
  ],
}

function setupMocks(response: unknown = validResponse) {
  mockGet.mockImplementation((url: string) => {
    if (url === '/api/v1/analytics/query') {
      return Promise.resolve({ data: response, error: undefined })
    }
    if (url === '/api/v1/pipeline-folders') {
      return Promise.resolve({ data: [], error: undefined })
    }
    if (url === '/api/v1/pipelines') {
      return Promise.resolve({
        data: { items: [], total: 0, page: 1, page_size: 100, next_cursor: null, has_more: false },
        error: undefined,
      })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
}

describe('serializeFilters', () => {
  it('maps a 7d timespan to UTC date_from/date_to', () => {
    const params = serializeFilters({ timespan: '7d', groupBy: 'day' }, FIXED_NOW)
    expect(params.date_to).toBe(utcDay(FIXED_NOW))
    expect(params.date_from).toBe(daysBefore(FIXED_NOW, 7))
    expect(params.group_by).toBe('day')
    expect(params.limit).toBe(1000)
  })

  it('emits day/week group_by from the granularity control', () => {
    const day = serializeFilters({ timespan: '7d', groupBy: 'day' }, FIXED_NOW)
    const week = serializeFilters({ timespan: '7d', groupBy: 'week' }, FIXED_NOW)
    expect(day.group_by).toBe('day')
    expect(week.group_by).toBe('week')
  })

  it('includes optional filters only when set', () => {
    const full = serializeFilters(
      {
        timespan: '30d',
        groupBy: 'week',
        dimension: 'trigger_type',
        triggerType: 'webhook',
        status: 'failed',
        pipelineId: 'p-1',
        folderId: 'f-1',
      },
      FIXED_NOW,
    )
    expect(full.group_by).toBe('week')
    expect(full.dimension).toBe('trigger_type')
    expect(full.trigger_type).toBe('webhook')
    expect(full.status).toBe('failed')
    expect(full.pipeline_id).toBe('p-1')
    expect(full.folder_id).toBe('f-1')

    const bare = serializeFilters({ timespan: '7d', groupBy: 'day' }, FIXED_NOW)
    expect(bare.dimension).toBeUndefined()
    expect(bare.trigger_type).toBeUndefined()
    expect(bare.status).toBeUndefined()
    expect(bare.pipeline_id).toBeUndefined()
    expect(bare.folder_id).toBeUndefined()
  })

  it('maps the 24h preset to a 24-hour UTC datetime window with hour granularity', () => {
    const day = serializeFilters({ timespan: '24h', groupBy: 'day' }, FIXED_NOW)
    expect(day.date_to).toContain('T')
    expect(day.date_from).toContain('T')
    expect(day.date_to).toBe(FIXED_NOW.toISOString())
    expect(day.date_from).toBe(hoursBefore(FIXED_NOW, 24))
    expect(day.group_by).toBe('hour')

    const week = serializeFilters({ timespan: '24h', groupBy: 'week' }, FIXED_NOW)
    expect(week.group_by).toBe('hour')
  })

  it('maps the 1h preset to a ~1-hour UTC datetime window with hour granularity', () => {
    const params = serializeFilters({ timespan: '1h', groupBy: 'day' }, FIXED_NOW)
    expect(params.date_to).toContain('T')
    expect(params.date_from).toContain('T')
    expect(params.date_to).toBe(FIXED_NOW.toISOString())
    expect(params.date_from).toBe(hoursBefore(FIXED_NOW, 1))
    expect(params.group_by).toBe('hour')
    expect(params.limit).toBe(1000)
  })

  it('maps the 3d preset to a 3-day UTC window', () => {
    const params = serializeFilters({ timespan: '3d', groupBy: 'day' }, FIXED_NOW)
    expect(params.date_to).toBe(utcDay(FIXED_NOW))
    expect(params.date_from).toBe(daysBefore(FIXED_NOW, 3))
    expect(params.group_by).toBe('day')
  })

  it('emits ISO day strings for day-granular timespans (3d+)', () => {
    for (const timespan of ['3d', '7d', '30d', '90d'] as const) {
      const params = serializeFilters({ timespan, groupBy: 'day' }, FIXED_NOW)
      expect(params.date_from).not.toContain('T')
      expect(params.date_to).not.toContain('T')
      expect(params.group_by).toBe('day')
    }
  })
})

describe('previousWindowParams', () => {
  it('shifts the window back by exactly one window', () => {
    const params = serializeFilters({ timespan: '7d', groupBy: 'day' }, FIXED_NOW)
    const prev = previousWindowParams(params)
    expect(prev.date_to).toBe(daysBefore(FIXED_NOW, 8))
    expect(prev.date_from).toBe(daysBefore(FIXED_NOW, 15))
    expect(prev.group_by).toBe(params.group_by)
  })

  it('shifts the 24h preset back by one 24-hour window, keeping ISO datetimes', () => {
    const params = serializeFilters({ timespan: '24h', groupBy: 'day' }, FIXED_NOW)
    const prev = previousWindowParams(params)
    expect(prev.date_to).toContain('T')
    expect(prev.date_from).toContain('T')
    expect(prev.date_to).toBe(params.date_from)
    expect(prev.date_from).toBe(hoursBefore(FIXED_NOW, 48))
    expect(prev.group_by).toBe('hour')
  })

  it('shifts the 1h preset back by one hour, keeping ISO datetimes', () => {
    const params = serializeFilters({ timespan: '1h', groupBy: 'day' }, FIXED_NOW)
    const prev = previousWindowParams(params)
    expect(prev.date_to).toContain('T')
    expect(prev.date_from).toContain('T')
    expect(prev.date_to).toBe(params.date_from)
    expect(prev.date_from).toBe(hoursBefore(FIXED_NOW, 2))
    expect(prev.group_by).toBe('hour')
  })
})

describe('buildChartOption', () => {
  it('maps an undimensioned series to a line chart', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', count: 3 },
      { date: '2026-08-02', count: 5 },
    ]
    const option = buildChartOption(series, 'count', 'day') as {
      xAxis: { data: string[] }
      series: Array<{ type: string; data: Array<number | null>; connectNulls: boolean; smooth: boolean }>
    }
    expect(option.xAxis.data).toEqual(['2026-08-01', '2026-08-02'])
    expect(option.series[0].type).toBe('line')
    expect(option.series[0].smooth).toBe(true)
    expect(option.series[0].connectNulls).toBe(false)
    expect(option.series[0].data).toEqual([3, 5])
  })

  it('maps a dimensioned series to a bar chart with the selected measure', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', key: 'manual', count: 2, total_cost_usd: 3 },
      { date: '2026-08-01', key: 'webhook', count: 4, total_cost_usd: 1.5 },
    ]
    const option = buildChartOption(series, 'cost', 'day') as {
      xAxis: { data: string[] }
      series: Array<{ type: string; data: Array<number | null> }>
    }
    expect(option.xAxis.data).toEqual(['manual', 'webhook'])
    expect(option.series[0].type).toBe('bar')
    expect(option.series[0].data).toEqual([3, 1.5])
  })

  it('aggregates a dimensioned series by key across dates', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', key: 'manual', count: 2, total_cost_usd: 1 },
      { date: '2026-08-02', key: 'manual', count: 4, total_cost_usd: 3 },
      { date: '2026-08-01', key: 'webhook', count: 3, total_cost_usd: 2 },
      { date: '2026-08-02', key: 'webhook', count: 1, total_cost_usd: 0.5 },
    ]
    const option = buildChartOption(series, 'count', 'day') as {
      xAxis: { data: string[] }
      series: Array<{ data: Array<number | null> }>
    }
    expect(option.xAxis.data).toEqual(['manual', 'webhook'])
    expect(option.series[0].data).toEqual([6, 4])
  })

  it('renders null (gap) rather than zero for pre-coverage buckets', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', count: 0, total_cost_usd: null },
      { date: '2026-08-02', count: 5, total_cost_usd: 2.5 },
    ]
    const option = buildChartOption(series, 'cost', 'day') as {
      series: Array<{ data: Array<number | null> }>
    }
    expect(option.series[0].data).toEqual([null, 2.5])
  })
})

describe('aggregateByKey', () => {
  it('sums counts, cost, and tokens per dimension key', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', key: 'manual', count: 2, total_cost_usd: 1, total_tokens: 100 },
      { date: '2026-08-02', key: 'manual', count: 4, total_cost_usd: 3, total_tokens: 300 },
    ]
    const agg = aggregateByKey(series)
    expect(agg).toHaveLength(1)
    expect(agg[0].key).toBe('manual')
    expect(agg[0].count).toBe(6)
    expect(agg[0].total_cost_usd).toBe(4)
    expect(agg[0].total_tokens).toBe(400)
  })

  it('weights avg_duration_ms and success_rate by count', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', key: 'manual', count: 2, avg_duration_ms: 1000, success_rate: 0.5 },
      { date: '2026-08-02', key: 'manual', count: 4, avg_duration_ms: 2000, success_rate: 0.75 },
    ]
    const agg = aggregateByKey(series)
    expect(agg).toHaveLength(1)
    expect(agg[0].avg_duration_ms).toBeCloseTo(1666.67, 1)
    expect(agg[0].success_rate).toBeCloseTo(0.6667, 3)
  })

  it('keeps count-less buckets cost as a sum', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', key: 'webhook', count: 0, total_cost_usd: 2.5 },
      { date: '2026-08-02', key: 'webhook', count: 0, total_cost_usd: 1.25 },
    ]
    const agg = aggregateByKey(series)
    expect(agg[0].count).toBe(0)
    expect(agg[0].total_cost_usd).toBe(3.75)
    expect(agg[0].avg_duration_ms).toBeNull()
    expect(agg[0].success_rate).toBeNull()
  })
})

describe('computeTrendDelta', () => {
  it('returns null when previous is zero (no baseline)', () => {
    expect(computeTrendDelta(5, 0)).toBeNull()
    expect(computeTrendDelta(0, 0)).toBeNull()
  })

  it('returns null when either value is missing', () => {
    expect(computeTrendDelta(null, 5)).toBeNull()
    expect(computeTrendDelta(5, undefined)).toBeNull()
  })

  it('returns up/down/flat for positive/negative/equal deltas', () => {
    expect(computeTrendDelta(10, 5)).toBe('up')
    expect(computeTrendDelta(5, 10)).toBe('down')
    expect(computeTrendDelta(5, 5)).toBe('flat')
  })
})

describe('formatDeltaPercent', () => {
  it('formats a signed delta percentage to 1dp', () => {
    expect(formatDeltaPercent(110, 100)).toBe('+10.0%')
    expect(formatDeltaPercent(90, 100)).toBe('-10.0%')
    expect(formatDeltaPercent(12.345, 10)).toBe('+23.5%')
  })

  it('returns null when the delta is not computable', () => {
    expect(formatDeltaPercent(0, 0)).toBeNull()
    expect(formatDeltaPercent(5, 0)).toBeNull()
    expect(formatDeltaPercent(null, 5)).toBeNull()
  })
})

describe('formatMeasureValue', () => {
  it('formats success_rate from the backend 0..1 fraction to a percentage', () => {
    expect(formatMeasureValue(0.75, 'success_rate')).toBe('75.0%')
    expect(formatMeasureValue(0.667, 'success_rate')).toBe('66.7%')
    expect(formatMeasureValue(0, 'success_rate')).toBe('0.0%')
    expect(formatMeasureValue(1, 'success_rate')).toBe('100.0%')
    expect(formatMeasureValue(null, 'success_rate')).toBe('—')
  })

  it('formats cost, tokens, duration, and count', () => {
    expect(formatMeasureValue(1.5, 'cost')).toBe('$1.50')
    expect(formatMeasureValue(1200, 'tokens')).toBe('1,200')
    expect(formatMeasureValue(2500, 'duration')).toBe('2500ms')
    expect(formatMeasureValue(5, 'count')).toBe('5')
    expect(formatMeasureValue(null, 'count')).toBe('—')
  })
})

describe('analytics store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetches and validates the query response', async () => {
    setupMocks(validResponse)
    const store = useAnalyticsStore()
    await store.fetchQuery()
    expect(store.results?.group_by).toBe('day')
    expect(store.buckets).toHaveLength(2)
    expect(store.flagOff).toBe(false)
    expect(store.error).toBeNull()
    expect(store.earliestAvailableDate).toBe('2026-08-01')
  })

  it('sets flagOff on a 402 feature-required error', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/analytics/query') {
        return Promise.resolve({
          data: undefined,
          error: {
            type: 'urn:problem:modulo:feature_required',
            title: 'Feature Not Available',
            status: 402,
            detail: 'Analytics is not enabled for your workspace',
          },
        })
      }
      if (url === '/api/v1/pipeline-folders') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [], total: 0, page: 1, page_size: 100, next_cursor: null, has_more: false },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const store = useAnalyticsStore()
    await store.fetchQuery()
    expect(store.flagOff).toBe(true)
    expect(store.error).not.toBeNull()
    expect(store.results).toBeNull()
  })

  it('sets a generic error when the response shape is invalid', async () => {
    setupMocks({ foo: 'bar' })
    const store = useAnalyticsStore()
    await store.fetchQuery()
    expect(store.error).not.toBeNull()
    expect(store.flagOff).toBe(false)
    expect(store.results).toBeNull()
  })

  it('commits only the latest query when responses resolve out of order', async () => {
    const older = {
      group_by: 'day',
      dimension: null,
      date_from: '2026-08-05',
      date_to: '2026-08-06',
      buckets: [{ date: '2026-08-05', count: 3 }],
    }
    const newer = {
      group_by: 'day',
      dimension: null,
      date_from: '2026-05-09',
      date_to: '2026-08-06',
      buckets: [{ date: '2026-07-01', count: 9 }],
    }
    let resolveOlder!: () => void
    let resolveNewer!: () => void
    let queryCalls = 0
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/analytics/query') {
        queryCalls += 1
        if (queryCalls === 1) {
          return new Promise((resolve) => {
            resolveOlder = () => resolve({ data: older, error: undefined })
          })
        }
        if (queryCalls === 2) {
          return new Promise((resolve) => {
            resolveNewer = () => resolve({ data: newer, error: undefined })
          })
        }
        return Promise.resolve({ data: { group_by: 'day', dimension: null, buckets: [] }, error: undefined })
      }
      if (url === '/api/v1/pipeline-folders') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [], total: 0, page: 1, page_size: 100, next_cursor: null, has_more: false },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const store = useAnalyticsStore()
    store.setFilters({ timespan: '24h' })
    const first = store.fetchQuery()
    store.setFilters({ timespan: '90d' })
    const second = store.fetchQuery()
    // The newer (second) request resolves first, then the older one.
    resolveNewer()
    await flushPromises()
    resolveOlder()
    await Promise.all([first, second])
    await flushPromises()
    expect(queryCalls).toBe(4)
    expect(store.results).toEqual(newer)
    expect(store.loading).toBe(false)
  })
})

describe('AnalyticsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders the page heading', async () => {
    setupMocks()
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Analytics')
    expect(wrapper.find('[data-testid="analytics-title"]').exists()).toBe(true)
  })

  it('renders the chart and trend table when data loads', async () => {
    setupMocks()
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    expect(wrapper.find('[data-testid="analytics-chart"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-table"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('2026-08-01')
    expect(wrapper.find('[data-testid="analytics-table"]').text()).toContain('3')
    expect(wrapper.find('[data-testid="analytics-table"]').text()).toContain('5')
  })

  it('renders the empty state with data-since when there is no data', async () => {
    setupMocks(emptyResponse)
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    expect(wrapper.find('[data-testid="analytics-empty-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No analytics data yet')
  })

  it('renders the not-enabled card on a 402 flag-off response', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/analytics/query') {
        return Promise.resolve({
          data: undefined,
          error: {
            type: 'urn:problem:modulo:feature_required',
            title: 'Feature Not Available',
            status: 402,
            detail: 'Analytics is not enabled for your workspace',
          },
        })
      }
      if (url === '/api/v1/pipeline-folders') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [], total: 0, page: 1, page_size: 100, next_cursor: null, has_more: false },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    expect(wrapper.find('[data-testid="analytics-not-enabled"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Analytics is not enabled for your workspace')
  })

  it('renders trend arrows in the table', async () => {
    const response = {
      group_by: 'day',
      dimension: null,
      date_from: '2026-07-30',
      date_to: '2026-08-06',
      buckets: [
        { date: '2026-08-01', count: 5, total_cost_usd: 2.5 },
        { date: '2026-08-02', count: 3, total_cost_usd: 1.5 },
      ],
    }
    const previousResponse = {
      group_by: 'day',
      dimension: null,
      date_from: '2026-07-23',
      date_to: '2026-07-29',
      buckets: [
        { date: '2026-08-01', count: 3, total_cost_usd: 1.5 },
        { date: '2026-08-02', count: 5, total_cost_usd: 2.5 },
      ],
    }
    let queryCalls = 0
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/analytics/query') {
        queryCalls += 1
        return Promise.resolve({ data: queryCalls === 1 ? response : previousResponse, error: undefined })
      }
      if (url === '/api/v1/pipeline-folders') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [], total: 0, page: 1, page_size: 100, next_cursor: null, has_more: false },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    const arrows = wrapper.findAll('[data-testid="analytics-trend-arrow"]')
    expect(arrows.length).toBe(2)
    expect(arrows[0].text()).toContain('▲')
    expect(arrows[1].text()).toContain('▼')
  })

  it('renders one table row per dimension key with deltas against the previous window', async () => {
    const response = {
      group_by: 'day',
      dimension: 'trigger_type',
      date_from: '2026-07-30',
      date_to: '2026-08-06',
      buckets: [
        { date: '2026-08-01', key: 'manual', count: 3 },
        { date: '2026-08-01', key: 'webhook', count: 4 },
        { date: '2026-08-02', key: 'manual', count: 2 },
        { date: '2026-08-02', key: 'webhook', count: 3 },
      ],
    }
    const previousResponse = {
      group_by: 'day',
      dimension: 'trigger_type',
      date_from: '2026-07-23',
      date_to: '2026-07-29',
      buckets: [
        { date: '2026-07-23', key: 'manual', count: 3 },
        { date: '2026-07-23', key: 'webhook', count: 9 },
        { date: '2026-07-24', key: 'manual', count: 1 },
        { date: '2026-07-24', key: 'webhook', count: 2 },
      ],
    }
    let queryCalls = 0
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/analytics/query') {
        queryCalls += 1
        return Promise.resolve({ data: queryCalls === 1 ? response : previousResponse, error: undefined })
      }
      if (url === '/api/v1/pipeline-folders') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [], total: 0, page: 1, page_size: 100, next_cursor: null, has_more: false },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    const tableText = wrapper.find('[data-testid="analytics-table"]').text()
    expect(tableText).toContain('manual')
    expect(tableText).toContain('webhook')
    // Aggregated per key: manual=5 vs 4 (up), webhook=7 vs 11 (down).
    const arrows = wrapper.findAll('[data-testid="analytics-trend-arrow"]')
    expect(arrows.length).toBe(2)
    expect(arrows[0].text()).toContain('▲')
    expect(arrows[1].text()).toContain('▼')
  })
})
