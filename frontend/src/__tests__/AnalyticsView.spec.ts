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
  previousWindowParams,
  type AnalyticsBucket,
} from '../stores/analytics'

const FIXED_NOW = new Date('2026-08-06T12:00:00Z')

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
      success_rate: 66.7,
    },
    {
      date: '2026-08-02',
      count: 5,
      total_cost_usd: 2.5,
      total_tokens: 2100,
      avg_duration_ms: 3000,
      success_rate: 80,
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
    expect(params.date_to).toBe('2026-08-06')
    expect(params.date_from).toBe('2026-07-30')
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
})

describe('previousWindowParams', () => {
  it('shifts the window back by exactly one window', () => {
    const params = serializeFilters({ timespan: '7d', groupBy: 'day' }, FIXED_NOW)
    const prev = previousWindowParams(params)
    expect(prev.date_to).toBe('2026-07-29')
    expect(prev.date_from).toBe('2026-07-22')
    expect(prev.group_by).toBe(params.group_by)
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
})
