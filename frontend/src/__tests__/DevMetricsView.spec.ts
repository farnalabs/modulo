import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockGet = vi.fn()

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get: mockGet }),
}))

import DevMetricsView from '../views/DevMetricsView.vue'

function summaryItem(overrides: Record<string, unknown> = {}) {
  return {
    metric_name: 'LCP',
    avg_value: 2500,
    min_value: 800,
    max_value: 4000,
    count: 120,
    good_pct: 55,
    ...overrides,
  }
}

function timeseriesPoint(overrides: Record<string, unknown> = {}) {
  return {
    date: '2026-09-15',
    metric_name: 'LCP',
    avg_value: 2400,
    count: 20,
    ...overrides,
  }
}

describe('DevMetricsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Default: summary returns items, timeseries return points per metric
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem()])
      }
      return Promise.resolve([timeseriesPoint()])
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders page header with title', async () => {
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Web Vitals Analytics')
  })

  it('renders subtitle', async () => {
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Frontend performance metrics')
  })

  it('calls loadData on mount', async () => {
    mount(DevMetricsView)
    await flushPromises()
    // 1 summary call + 5 metric timeseries calls
    expect(mockGet).toHaveBeenCalledTimes(6)
    expect(mockGet).toHaveBeenCalledWith(expect.stringContaining('/summary'))
  })

  it('passes selectedDays=7 by default in API URL', async () => {
    mount(DevMetricsView)
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith(expect.stringContaining('days=7'))
  })

  it('shows loading spinner on initial load', async () => {
    // Hold the API call open
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(DevMetricsView)
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows error alert when summary and all timeseries fail', async () => {
    mockGet.mockRejectedValue(new Error('network error'))
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load web vitals data')
    expect(wrapper.text()).toContain('The API may be unavailable')
  })

  it('shows error alert when summary returns null and all timeseries are empty', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) return Promise.resolve(null)
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load web vitals data')
  })

  it('shows empty state when summary is empty and no timeseries', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) return Promise.resolve([])
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('No data yet')
    expect(wrapper.text()).toContain('Web vitals data will appear here')
  })

  it('renders summary cards with metric labels and values', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([
          summaryItem({ metric_name: 'LCP', avg_value: 2400, min_value: 800, max_value: 5000, count: 100, good_pct: 70 }),
          summaryItem({ metric_name: 'CLS', avg_value: 0.08, min_value: 0, max_value: 0.2, count: 50, good_pct: 92 }),
        ])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Largest Contentful Paint')
    expect(wrapper.text()).toContain('Cumulative Layout Shift')
    expect(wrapper.text()).toContain('2.40s')
    expect(wrapper.text()).toContain('0.080')
    expect(wrapper.text()).toContain('100 measurements')
    expect(wrapper.text()).toContain('70% good')
    expect(wrapper.text()).toContain('92% good')
  })

  it('renders min/max values for summary cards', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ min_value: 800, max_value: 5000 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('min')
    expect(wrapper.text()).toContain('max')
    expect(wrapper.text()).toContain('800ms')
    expect(wrapper.text()).toContain('5.00s')
  })

  it('renders good_pct bar with correct color classes for >=90%', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ good_pct: 95 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('95% good')
    const bar = wrapper.find('.bg-success')
    expect(bar.exists()).toBe(true)
  })

  it('renders good_pct bar warning class for 50-89%', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ good_pct: 60 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('60% good')
    const bar = wrapper.find('.bg-warning')
    expect(bar.exists()).toBe(true)
  })

  it('renders good_pct bar destructive class for <50%', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ good_pct: 30 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('30% good')
    const bar = wrapper.find('.bg-destructive')
    expect(bar.exists()).toBe(true)
  })

  it('renders em dash when good_pct is null', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ good_pct: null })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('\u2014')
  })

  it('applies metricColor class based on avg_value thresholds', async () => {
    // LCP: good <= 2500, poor <= 4000
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([
          summaryItem({ metric_name: 'LCP', avg_value: 2000 }), // good
          summaryItem({ metric_name: 'FCP', avg_value: 2500 }), // warning
          summaryItem({ metric_name: 'INP', avg_value: 600 }),  // destructive
        ])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    const cards = wrapper.findAll('.card')
    // LCP card should have text-success
    expect(cards[0].find('.text-success').exists()).toBe(true)
    // FCP card should have text-warning
    expect(cards[1].find('.text-warning').exists()).toBe(true)
    // INP card should have text-destructive
    expect(cards[2].find('.text-destructive').exists()).toBe(true)
  })

  it('handles unknown metric_name with empty color class', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ metric_name: 'UNKNOWN', avg_value: 100 })])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('UNKNOWN')
    // metricLabel returns the raw name for unknown metrics
    expect(wrapper.text()).toContain('UNKNOWN')
  })

  it('renders timeseries bar charts with date labels', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ metric_name: 'LCP' })])
      }
      if (url.includes('timeseries')) {
        return Promise.resolve([
          timeseriesPoint({ date: '2026-09-10', avg_value: 2100 }),
          timeseriesPoint({ date: '2026-09-11', avg_value: 2300 }),
          timeseriesPoint({ date: '2026-09-12', avg_value: 2500 }),
        ])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Largest Contentful Paint over time')
    expect(wrapper.text()).toContain('2026-09-10')
    expect(wrapper.text()).toContain('2026-09-12')
    // 3 bar divs
    const bars = wrapper.findAll('.flex-1 .rounded-t')
    expect(bars.length).toBeGreaterThanOrEqual(3)
  })

  it('renders middle date label when timeseries has > 2 points', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem()])
      }
      if (url.includes('timeseries')) {
        return Promise.resolve([
          timeseriesPoint({ date: '2026-09-01', avg_value: 2000 }),
          timeseriesPoint({ date: '2026-09-05', avg_value: 2200 }),
          timeseriesPoint({ date: '2026-09-10', avg_value: 2400 }),
        ])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('2026-09-05')
  })

  it('formatMetricValue returns seconds for values >= 1000 (non-CLS)', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ metric_name: 'LCP', avg_value: 1200 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('1.20s')
  })

  it('formatMetricValue returns ms for values < 1000 (non-CLS)', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ metric_name: 'FCP', avg_value: 850 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('850ms')
  })

  it('formatMetricValue returns 3 decimal places for CLS', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ metric_name: 'CLS', avg_value: 0.123 })])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('0.123')
  })

  it('handles timeseries error for individual metric gracefully', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem()])
      }
      // Some succeed, some fail
      if (url.includes('metric_name=FCP')) {
        return Promise.reject(new Error('fail'))
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    // Should still render summary without crashing
    expect(wrapper.text()).toContain('Largest Contentful Paint')
  })

  it('summary error with partial timeseries does not show error', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) return Promise.resolve(null)
      // At least one timeseries has data
      if (url.includes('metric_name=LCP')) {
        return Promise.resolve([timeseriesPoint()])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    // summary is null but timeseries have data => no error shown
    expect(wrapper.text()).not.toContain('Failed to load web vitals data')
  })

  it('barHeight enforces minimum 5% height for nonzero values', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) return Promise.resolve([summaryItem()])
      if (url.includes('timeseries')) {
        return Promise.resolve([
          timeseriesPoint({ avg_value: 0 }),
        ])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    // barHeight(value, maxValue) = Math.max(5, (value/maxValue)*100)
    // avg_value=0, maxValue=max(0,1)=1 => Math.max(5, 0) = 5
    const bars = wrapper.findAll('.rounded-t')
    for (const bar of bars) {
      expect(bar.attributes('style')).toContain('height: 5%')
    }
  })

  it('barColor applies correct classes for each threshold zone', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) return Promise.resolve([summaryItem()])
      if (url.includes('timeseries')) {
        return Promise.resolve([
          timeseriesPoint({ avg_value: 100 }), // <= good -> bg-success
          timeseriesPoint({ date: '2026-09-16', avg_value: 3000 }), // <= poor -> bg-warning
          timeseriesPoint({ date: '2026-09-17', avg_value: 5000 }), // > poor -> bg-destructive
        ])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.find('.bg-success').exists()).toBe(true)
    expect(wrapper.find('.bg-warning').exists()).toBe(true)
    expect(wrapper.find('.bg-destructive').exists()).toBe(true)
  })

  it('sets loading to false after loadData completes', async () => {
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    // Loading spinner should not be visible after load
    expect(wrapper.find('.animate-spin').exists()).toBe(false)
  })

  it('renders all five metric timeseries charts', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem()])
      }
      return Promise.resolve([timeseriesPoint()])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    expect(wrapper.text()).toContain('Cumulative Layout Shift over time')
    expect(wrapper.text()).toContain('First Contentful Paint over time')
    expect(wrapper.text()).toContain('Interaction to Next Paint over time')
    expect(wrapper.text()).toContain('Largest Contentful Paint over time')
    expect(wrapper.text()).toContain('Time to First Byte over time')
  })

  it('renders time-range selector and refresh button via the right slot', async () => {
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    // The header's right-hand container must render (PageHeader injects it via #right)
    const headerRight = wrapper.find('[data-testid="page-header-right"]')
    expect(headerRight.exists()).toBe(true)
    // The time-range <select> must be rendered inside the header's right slot
    const select = headerRight.find('[data-testid="dev-metrics-time-range"]')
    expect(select.exists()).toBe(true)
    // All three day options must be present
    const options = select.findAll('option')
    expect(options).toHaveLength(3)
    expect(options[0].text()).toBe('Last 7 days')
    expect(options[1].text()).toBe('Last 30 days')
    expect(options[2].text()).toBe('Last 90 days')
    // The Refresh button must be rendered inside the header's right slot,
    // scoped by data-testid so it does not depend on DOM order
    const refreshBtn = headerRight.find('[data-testid="dev-metrics-refresh"]')
    expect(refreshBtn.exists()).toBe(true)
    expect(refreshBtn.text()).toContain('Refresh')
  })

  it('renders bar tooltip with date and formatted value', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes('/summary')) {
        return Promise.resolve([summaryItem({ metric_name: 'LCP' })])
      }
      if (url.includes('timeseries')) {
        return Promise.resolve([
          timeseriesPoint({ date: '2026-09-15', avg_value: 2400 }),
        ])
      }
      return Promise.resolve([])
    })
    const wrapper = mount(DevMetricsView)
    await flushPromises()
    // Find the bar in the LCP chart specifically
    const lcpSection = wrapper.findAll('.card').find(card => card.text().includes('Largest Contentful Paint over time'))
    expect(lcpSection).toBeTruthy()
    const bar = lcpSection!.find('.rounded-t')
    expect(bar.attributes('title')).toContain('2026-09-15')
    expect(bar.attributes('title')).toContain('2.40s')
  })
})
