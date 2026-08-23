import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive } from 'vue'

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
import { formatDateShortWithTime } from '../lib/formatDate'
import {
  useAnalyticsStore,
  serializeFilters,
  buildChartOption,
  computeTrendDelta,
  formatDeltaPercent,
  formatMeasureValue,
  aggregateByKey,
  formatBucketDate,
  previousWindowParams,
  applyQueryParamsToFilters,
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

  it('emits error_code when set and omits it otherwise', () => {
    const full = serializeFilters(
      { timespan: '7d', groupBy: 'day', errorCode: 'executor_stalled' },
      FIXED_NOW,
    )
    expect(full.error_code).toBe('executor_stalled')

    const bare = serializeFilters({ timespan: '7d', groupBy: 'day' }, FIXED_NOW)
    expect(bare.error_code).toBeUndefined()
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

  it('honours an explicit deep-link date range over the timespan derivation', () => {
    const params = serializeFilters(
      {
        timespan: '7d',
        groupBy: 'week',
        dateFrom: '2026-06-01',
        dateTo: '2026-08-06',
      },
      FIXED_NOW,
    )
    expect(params.date_from).toBe('2026-06-01')
    expect(params.date_to).toBe('2026-08-06')
    expect(params.group_by).toBe('week')
  })

  it('honours an hour-granular deep-link range with ISO datetimes', () => {
    const params = serializeFilters(
      {
        timespan: '7d',
        groupBy: 'hour',
        dateFrom: '2026-08-06T08:00:00+00:00',
        dateTo: '2026-08-06T12:00:00+00:00',
      },
      FIXED_NOW,
    )
    expect(params.group_by).toBe('hour')
    expect(params.date_from).toBe('2026-08-06T08:00:00+00:00')
    expect(params.date_to).toBe('2026-08-06T12:00:00+00:00')
  })
})

describe('applyQueryParamsToFilters', () => {
  const base = { timespan: '7d' as const, groupBy: 'day' as const }

  it('maps a deep-link query onto filters', () => {
    const { filters, applied } = applyQueryParamsToFilters(
      {
        group_by: 'week',
        date_from: '2026-06-01',
        date_to: '2026-08-06',
        dimension: 'trigger_type',
        trigger_type: 'webhook',
        status: 'failed',
        pipeline_id: 'p-1',
        folder_id: 'f-1',
      },
      base,
    )
    expect(applied).toBe(true)
    expect(filters.groupBy).toBe('week')
    expect(filters.dateFrom).toBe('2026-06-01')
    expect(filters.dateTo).toBe('2026-08-06')
    expect(filters.dimension).toBe('trigger_type')
    expect(filters.triggerType).toBe('webhook')
    expect(filters.status).toBe('failed')
    expect(filters.pipelineId).toBe('p-1')
    expect(filters.folderId).toBe('f-1')
    // Timespan is untouched — the explicit range overrides it.
    expect(filters.timespan).toBe('7d')
  })

  it('ignores unknown group_by/dimension values and malformed dates', () => {
    const { filters, applied } = applyQueryParamsToFilters(
      {
        group_by: 'fortnight',
        dimension: 'bogus',
        date_from: 'not-a-date',
        date_to: '2026-08-06',
      },
      base,
    )
    expect(applied).toBe(false)
    expect(filters).toEqual(base)
  })

  it('rejects a partial date range (only one bound set)', () => {
    const { filters, applied } = applyQueryParamsToFilters(
      { date_from: '2026-06-01' },
      base,
    )
    expect(applied).toBe(false)
    expect(filters.dateFrom).toBeUndefined()
  })

  it('honours the error_code deep-link filter and dimension=error_code', () => {
    const { filters, applied } = applyQueryParamsToFilters(
      {
        dimension: 'error_code',
        error_code: 'executor_stalled',
        date_from: '2026-06-01',
        date_to: '2026-08-06',
      },
      base,
    )
    expect(applied).toBe(true)
    expect(filters.dimension).toBe('error_code')
    expect(filters.errorCode).toBe('executor_stalled')
  })

  it('round-trips an error_code deep link through applyQueryParamsToFilters + serializeFilters', () => {
    const { filters } = applyQueryParamsToFilters(
      { dimension: 'error_code', error_code: 'executor_stalled' },
      base,
    )
    const params = serializeFilters(filters, FIXED_NOW)
    expect(params.dimension).toBe('error_code')
    expect(params.error_code).toBe('executor_stalled')
  })
})

describe('formatBucketDate', () => {
  it('formats hour-granular bucket timestamps as UTC in the viewer timezone', () => {
    // Backend hour buckets are naive-UTC ("2026-08-06T14:00:00"); formatBucketDate
    // must treat them as UTC (append Z) so formatDateShortWithTime renders the
    // correct local clock time regardless of the viewer timezone.
    const formatted = formatBucketDate('2026-08-06T14:00:00')
    expect(formatted).toBe(formatDateShortWithTime('2026-08-06T14:00:00Z'))
    expect(formatted).not.toContain('2026-08-06T14:00:00')
  })

  it('leaves day-granular bucket dates unchanged', () => {
    expect(formatBucketDate('2026-08-01')).toBe('2026-08-01')
    expect(formatBucketDate(null)).toBe('')
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

  it('humanizes hour-bucket dates on the chart axis', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-06T14:00:00', count: 3 },
      { date: '2026-08-06T15:00:00', count: 5 },
    ]
    const option = buildChartOption(series, 'count', 'hour') as {
      xAxis: { data: string[] }
    }
    expect(option.xAxis.data).toEqual([
      formatDateShortWithTime('2026-08-06T14:00:00Z'),
      formatDateShortWithTime('2026-08-06T15:00:00Z'),
    ])
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

  it('weights success_rate only across buckets that report it', () => {
    const series: AnalyticsBucket[] = [
      { date: '2026-08-01', key: 'manual', count: 2, success_rate: 0.5 },
      { date: '2026-08-02', key: 'manual', count: 4, success_rate: 0.75 },
      { date: '2026-08-03', key: 'manual', count: 4, success_rate: null },
    ]
    const agg = aggregateByKey(series)
    expect(agg).toHaveLength(1)
    expect(agg[0].count).toBe(10)
    expect(agg[0].success_rate).toBeCloseTo(0.6667, 3)
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

  it('exposes the effective granularity: hour for 1h/24h, otherwise the selected day/week', () => {
    const store = useAnalyticsStore()
    expect(store.groupBy).toBe('day')
    store.setFilters({ timespan: '24h' })
    expect(store.groupBy).toBe('hour')
    store.setFilters({ timespan: '1h' })
    expect(store.groupBy).toBe('hour')
    store.setFilters({ timespan: '7d', groupBy: 'week' })
    expect(store.groupBy).toBe('week')
  })

  it('applies deep-link query params and reflects the explicit range in the effective granularity', () => {
    const store = useAnalyticsStore()
    const applied = store.applyQueryParams({
      group_by: 'hour',
      date_from: '2026-08-06T08:00:00+00:00',
      date_to: '2026-08-06T12:00:00+00:00',
    })
    expect(applied).toBe(true)
    expect(store.filters.dateFrom).toBe('2026-08-06T08:00:00+00:00')
    expect(store.groupBy).toBe('hour')
  })

  it('clears the explicit deep-link range when the user changes the timespan', () => {
    const store = useAnalyticsStore()
    store.applyQueryParams({ date_from: '2026-06-01', date_to: '2026-08-06', group_by: 'day' })
    expect(store.filters.dateFrom).toBe('2026-06-01')
    store.setFilters({ timespan: '30d' })
    expect(store.filters.dateFrom).toBeUndefined()
    expect(store.filters.dateTo).toBeUndefined()
  })

  it('keeps the explicit deep-link range when a non-timespan filter changes', () => {
    const store = useAnalyticsStore()
    store.applyQueryParams({ date_from: '2026-06-01', date_to: '2026-08-06', group_by: 'day' })
    expect(store.filters.dateFrom).toBe('2026-06-01')
    // The filter bar always re-emits the current timespan; a same-timespan patch
    // (e.g. only status changed) must NOT drop the deep-link date range.
    store.setFilters({ timespan: '7d', status: 'failed' })
    expect(store.filters.dateFrom).toBe('2026-06-01')
    expect(store.filters.dateTo).toBe('2026-08-06')
    expect(store.filters.status).toBe('failed')
  })

  it('applies an error_code deep link and re-serializes it into the query', async () => {
    setupMocks(validResponse)
    const store = useAnalyticsStore()
    const applied = store.applyQueryParams({
      dimension: 'error_code',
      error_code: 'executor_stalled',
      date_from: '2026-06-01',
      date_to: '2026-08-06',
    })
    expect(applied).toBe(true)
    expect(store.filters.errorCode).toBe('executor_stalled')
    expect(store.filters.dimension).toBe('error_code')
    await store.fetchQuery()
    const queryCall = mockGet.mock.calls.find((c) => c[0] === '/api/v1/analytics/query')
    const q = (queryCall?.[1] as { params: { query: Record<string, unknown> } } | undefined)?.params.query
    expect(q?.error_code).toBe('executor_stalled')
    expect(q?.dimension).toBe('error_code')
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

  it('hides the day/week group-by control and shows a disabled Hour pill for hour-granular timespans', async () => {
    setupMocks()
    const store = useAnalyticsStore()
    store.setFilters({ timespan: '24h' })
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    expect(wrapper.find('[data-testid="analytics-group-by-day"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="analytics-group-by-week"]').exists()).toBe(false)
    const hour = wrapper.find('[data-testid="analytics-group-by-hour"]')
    expect(hour.exists()).toBe(true)
    expect(hour.attributes('disabled')).toBeDefined()
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

  it('renders human-readable labels for error_code dimension keys in the table and chart', async () => {
    const response = {
      group_by: 'day',
      dimension: 'error_code',
      date_from: '2026-07-30',
      date_to: '2026-08-06',
      buckets: [
        { date: '2026-08-01', key: 'agent.stall', count: 3 },
        { date: '2026-08-01', key: 'harness.worker_failed', count: 1 },
        { date: '2026-08-02', key: 'agent.stall', count: 2 },
        { date: '2026-08-02', key: 'harness.worker_failed', count: 4 },
      ],
    }
    const previousResponse = {
      group_by: 'day',
      dimension: 'error_code',
      date_from: '2026-07-23',
      date_to: '2026-07-29',
      buckets: [
        { date: '2026-07-23', key: 'agent.stall', count: 1 },
        { date: '2026-07-23', key: 'harness.worker_failed', count: 2 },
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
    const store = useAnalyticsStore()
    store.setFilters({ dimension: 'error_code' })
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    // The chart still renders for a dimensioned (bar) series.
    expect(wrapper.find('[data-testid="analytics-chart"]').exists()).toBe(true)
    const tableText = wrapper.find('[data-testid="analytics-table"]').text()
    expect(tableText).toContain('Worker claimed run but dispatched no node (recovered by re-dispatch)')
    expect(tableText).toContain('Worker failed')
    expect(tableText).not.toContain('agent.stall')
    expect(tableText).not.toContain('harness.worker_failed')
  })

  it('pre-filters from a deep-link query on mount (e.g. Remy /analytics link)', async () => {
    setupMocks()
    const { useRoute } = await import('vue-router')
    const routeMock = vi.mocked(useRoute)
    routeMock.mockImplementation(() =>
      ({
        query: {
          group_by: 'week',
          date_from: '2026-06-01',
          date_to: '2026-08-06',
          pipeline_id: 'p-1',
        },
      }) as never,
    )
    mount(AnalyticsView)
    await flushPromises()
    const store = useAnalyticsStore()
    expect(store.filters.groupBy).toBe('week')
    expect(store.filters.dateFrom).toBe('2026-06-01')
    expect(store.filters.dateTo).toBe('2026-08-06')
    expect(store.filters.pipelineId).toBe('p-1')
    const queryCall = mockGet.mock.calls.find((c) => c[0] === '/api/v1/analytics/query')
    const q = (queryCall?.[1] as { params: { query: Record<string, unknown> } } | undefined)?.params.query
    expect(q?.group_by).toBe('week')
    expect(q?.date_from).toBe('2026-06-01')
    expect(q?.pipeline_id).toBe('p-1')
    // Restore the shared useRoute mock for other tests.
    routeMock.mockImplementation(() => ({ query: {} }) as never)
  })

  it('re-applies the deep-link query on same-route navigation (no remount)', async () => {
    setupMocks()
    const { useRoute } = await import('vue-router')
    const routeMock = vi.mocked(useRoute)
    const routeValue = reactive({ query: {} as Record<string, string> })
    routeMock.mockImplementation(() => routeValue as never)
    mount(AnalyticsView)
    await flushPromises()
    const store = useAnalyticsStore()
    expect(store.filters.groupBy).toBe('day')
    // Simulate a Remy deep-link navigation while already on /analytics: the
    // component is reused, so only the route-query watcher can apply it.
    routeValue.query = {
      group_by: 'week',
      date_from: '2026-06-01',
      date_to: '2026-08-06',
      error_code: 'executor_stalled',
    }
    await nextTick()
    await flushPromises()
    expect(store.filters.groupBy).toBe('week')
    expect(store.filters.dateFrom).toBe('2026-06-01')
    expect(store.filters.errorCode).toBe('executor_stalled')
    const queryCalls = mockGet.mock.calls.filter((c) => c[0] === '/api/v1/analytics/query')
    const lastQuery = (queryCalls.at(-1)?.[1] as { params: { query: Record<string, unknown> } } | undefined)
      ?.params.query
    expect(lastQuery?.error_code).toBe('executor_stalled')
    expect(lastQuery?.group_by).toBe('week')
    // Restore the shared useRoute mock for other tests.
    routeMock.mockImplementation(() => ({ query: {} }) as never)
  })

  it('debounces the error-code text filter so a full query does not fire per keystroke', async () => {
    setupMocks()
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    const errorCodeInput = wrapper.find('[data-testid="analytics-filter-error-code"]')
    expect(errorCodeInput.exists()).toBe(true)

    vi.useFakeTimers()
    await errorCodeInput.setValue('exec')
    await errorCodeInput.setValue('executor')
    await errorCodeInput.setValue('executor_stalled')
    await nextTick()

    // Debounce window has not elapsed — no query carrying the error code yet.
    let queryCalls = mockGet.mock.calls.filter((c) => c[0] === '/api/v1/analytics/query')
    let lastQuery = (queryCalls.at(-1)?.[1] as { params: { query: Record<string, unknown> } } | undefined)?.params.query
    expect(lastQuery?.error_code).toBeUndefined()

    vi.advanceTimersByTime(300)
    vi.useRealTimers()
    await flushPromises()
    await nextTick()

    // Exactly the settled value fires, once, after the debounce window.
    queryCalls = mockGet.mock.calls.filter((c) => c[0] === '/api/v1/analytics/query')
    lastQuery = (queryCalls.at(-1)?.[1] as { params: { query: Record<string, unknown> } } | undefined)?.params.query
    expect(lastQuery?.error_code).toBe('executor_stalled')
    wrapper.unmount()
  })

  it('still fetches immediately when a select-based filter changes (not debounced)', async () => {
    setupMocks()
    const wrapper = mount(AnalyticsView)
    await flushPromises()
    const statusSelect = wrapper.find('[data-testid="analytics-filter-status"]')
    expect(statusSelect.exists()).toBe(true)

    await statusSelect.setValue('failed')
    await flushPromises()

    const queryCalls = mockGet.mock.calls.filter((c) => c[0] === '/api/v1/analytics/query')
    const lastQuery = (queryCalls.at(-1)?.[1] as { params: { query: Record<string, unknown> } } | undefined)?.params.query
    expect(lastQuery?.status).toBe('failed')
    wrapper.unmount()
  })
})
