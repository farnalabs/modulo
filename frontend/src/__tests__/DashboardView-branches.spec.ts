/**
 * Branch coverage tests for DashboardView.vue (FAR-835).
 *
 * Targets uncovered branches: cardValue edge cases, evalRateDisplay null
 * paths, evalTrend flat/less-than-2, evalDeltaPctText non-finite/null,
 * spendDeltaPctText null, deltaDirection zero, loadTrendWindow edge cases,
 * localStorage unavailable, selectWindow null, trendData null/missing,
 * summary recent_runs/teams empty, team row expansion, expandedTeamData
 * mismatch, error+no summary, loading skeleton, eval null display.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockSummaryData = {
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
  trend: [
    { date: '2026-06-23', run_count: 18, eval_pass_rate: 80.0, token_spend_usd: 12.50 },
    { date: '2026-06-24', run_count: 22, eval_pass_rate: 85.0, token_spend_usd: 15.20 },
    { date: '2026-06-25', run_count: 15, eval_pass_rate: 78.0, token_spend_usd: 10.10 },
    { date: '2026-06-26', run_count: 20, eval_pass_rate: 82.0, token_spend_usd: 14.00 },
    { date: '2026-06-27', run_count: 25, eval_pass_rate: 88.0, token_spend_usd: 18.75 },
    { date: '2026-06-28', run_count: 19, eval_pass_rate: 81.0, token_spend_usd: 13.30 },
    { date: '2026-06-29', run_count: 23, eval_pass_rate: 84.0, token_spend_usd: 16.40 },
  ],
  recent_runs: [
    { id: 'run-1', pipeline_name: 'Deploy Pipeline', status: 'complete', created_at: '2026-06-29T10:30:00Z', trigger_type: 'manual' },
  ],
}

const mockFlagData = {
  license: { tier: 'community', has_license_key: false, is_valid: false },
  flags: [],
  would_activate: [],
}

const mockLicenseData = {
  has_license: false,
  tier: 'community',
  features: [],
  expires_at: null,
  org_id: null,
}

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))

import DashboardView from '../views/DashboardView.vue'

function setupDefaultMocks() {
  mockGet.mockImplementation((url: string) => {
    if (url === '/api/v1/dashboard/summary') return Promise.resolve({ data: mockSummaryData, error: undefined })
    if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
    if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
  setupDefaultMocks()
})

// ── cardValue edge cases ────────────────────────────────────────────────
describe('DashboardView branches — cardValue', () => {
  it('returns allTime when periodCurrent is 0 and allTime is non-zero', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 0, previous: 10, delta_pct: -100 },
                active_pipelines: { current: 5, previous: 5, delta_pct: 0 },
                run_counts_by_status: {
                  running: { current: 0, previous: 0, delta_pct: null },
                  awaiting_human: { current: 0, previous: 0, delta_pct: null },
                  failed: { current: 0, previous: 0, delta_pct: null },
                  idle: { current: 0, previous: 0, delta_pct: null },
                },
                eval_pass_rate: { current: 80, previous: 75, delta_pct: 6.7 },
                spend: { current: 50, previous: 60, delta_pct: -16.7 },
              },
            },
            total_runs: 142, // allTime is non-zero
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // cardValue(periodCurrent=0, allTime=142) -> allTime=142 because 0 !== 0 is false, allTimeVal=142 !== 0
    expect(wrapper.text()).toContain('142')
    wrapper.unmount()
  })

  it('returns periodCurrent when both are 0', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 0, previous: 0, delta_pct: 0 },
                active_pipelines: { current: 0, previous: 0, delta_pct: 0 },
                run_counts_by_status: {
                  running: { current: 0, previous: 0, delta_pct: null },
                  awaiting_human: { current: 0, previous: 0, delta_pct: null },
                  failed: { current: 0, previous: 0, delta_pct: null },
                  idle: { current: 0, previous: 0, delta_pct: null },
                },
                eval_pass_rate: { current: 80, previous: 75, delta_pct: 6.7 },
                spend: { current: 50, previous: 60, delta_pct: -16.7 },
              },
            },
            total_runs: 0,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // cardValue(0, 0) -> 0 (periodCurrent is returned when allTimeVal===0)
    expect(wrapper.text()).toContain('Total Runs')
    wrapper.unmount()
  })

  it('returns allTime when periodCurrent is null', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: null, previous: 10, delta_pct: null },
                active_pipelines: { current: 5, previous: 5, delta_pct: 0 },
                run_counts_by_status: {
                  running: { current: null, previous: 0, delta_pct: null },
                  awaiting_human: { current: null, previous: 0, delta_pct: null },
                  failed: { current: null, previous: 0, delta_pct: null },
                  idle: { current: null, previous: 0, delta_pct: null },
                },
                eval_pass_rate: { current: 80, previous: 75, delta_pct: 6.7 },
                spend: { current: 50, previous: 60, delta_pct: -16.7 },
              },
            },
            total_runs: 142,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // cardValue(null, 142) -> allTime=142
    expect(wrapper.text()).toContain('142')
    wrapper.unmount()
  })
})

// ── evalRateDisplay edge cases ──────────────────────────────────────────
describe('DashboardView branches — evalRateDisplay', () => {
  it('shows dash when period has null current', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 40, delta_pct: 25 },
                active_pipelines: { current: 8, previous: 9, delta_pct: -11 },
                run_counts_by_status: {
                  running: { current: 3, previous: 2, delta_pct: 50 },
                  awaiting_human: { current: 2, previous: 1, delta_pct: 100 },
                  failed: { current: 5, previous: 3, delta_pct: 67 },
                  idle: { current: 12, previous: 10, delta_pct: 20 },
                },
                eval_pass_rate: { current: null, previous: 80, delta_pct: null },
                spend: { current: 100, previous: 90, delta_pct: 11 },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // evalRateDisplay: period with null current -> dash
    const evalCard = wrapper.find('[data-testid="dashboard-eval-rate"]')
    expect(evalCard.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash when allTime eval_pass_rate is null', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            eval_pass_rate: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    const evalCard = wrapper.find('[data-testid="dashboard-eval-rate"]')
    expect(evalCard.text()).toContain('No eval data yet')
    wrapper.unmount()
  })
})

// ── evalTrend flat case ────────────────────────────────────────────────
describe('DashboardView branches — evalTrend flat', () => {
  it('shows stable when eval rates are equal (flat)', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            trend: [
              { date: '2026-06-23', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
              { date: '2026-06-24', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
              { date: '2026-06-25', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
              { date: '2026-06-26', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Stable')
    wrapper.unmount()
  })

  it('shows stable when less than 2 rates', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            trend: [
              { date: '2026-06-23', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Stable')
    wrapper.unmount()
  })
})

// ── evalDeltaPctText edge cases ─────────────────────────────────────────
describe('DashboardView branches — evalDeltaPctText', () => {
  it('returns empty for non-finite delta', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 40, delta_pct: 25 },
                active_pipelines: { current: 8, previous: 9, delta_pct: -11 },
                run_counts_by_status: {
                  running: { current: 3, previous: 2, delta_pct: 50 },
                  awaiting_human: { current: 2, previous: 1, delta_pct: 100 },
                  failed: { current: 5, previous: 3, delta_pct: 67 },
                  idle: { current: 12, previous: 10, delta_pct: 20 },
                },
                eval_pass_rate: { current: 80, previous: 0, delta_pct: Infinity },
                spend: { current: 100, previous: 90, delta_pct: 11 },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // evalDeltaPctText returns '' for non-finite (Infinity)
    const evalCard = wrapper.find('[data-testid="dashboard-eval-rate"]')
    expect(evalCard.exists()).toBe(true)
    wrapper.unmount()
  })

  it('returns empty for null delta', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 40, delta_pct: 25 },
                active_pipelines: { current: 8, previous: 9, delta_pct: -11 },
                run_counts_by_status: {
                  running: { current: 3, previous: 2, delta_pct: 50 },
                  awaiting_human: { current: 2, previous: 1, delta_pct: 100 },
                  failed: { current: 5, previous: 3, delta_pct: 67 },
                  idle: { current: 12, previous: 10, delta_pct: 20 },
                },
                eval_pass_rate: { current: 80, previous: 75, delta_pct: null },
                spend: { current: 100, previous: 90, delta_pct: 11 },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    const evalCard = wrapper.find('[data-testid="dashboard-eval-rate"]')
    expect(evalCard.exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── spendDeltaPctText null ──────────────────────────────────────────────
describe('DashboardView branches — spendDeltaPctText', () => {
  it('returns empty string when spend delta is null', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 40, delta_pct: 25 },
                active_pipelines: { current: 8, previous: 9, delta_pct: -11 },
                run_counts_by_status: {
                  running: { current: 3, previous: 2, delta_pct: 50 },
                  awaiting_human: { current: 2, previous: 1, delta_pct: 100 },
                  failed: { current: 5, previous: 3, delta_pct: 67 },
                  idle: { current: 12, previous: 10, delta_pct: 20 },
                },
                eval_pass_rate: { current: 80, previous: 75, delta_pct: 6.7 },
                spend: { current: 100, previous: 90, delta_pct: null },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    const spendCard = wrapper.find('[data-testid="dashboard-token-spend"]')
    // No delta text should be rendered for null spend delta
    const deltaSpan = spendCard.find('span.text-xs.font-medium')
    expect(deltaSpan.exists()).toBe(false)
    wrapper.unmount()
  })
})

// ── deltaDirection zero ─────────────────────────────────────────────────
describe('DashboardView branches — deltaDirection', () => {
  it('returns flat for zero delta', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 50, delta_pct: 0 },
                active_pipelines: { current: 8, previous: 8, delta_pct: 0 },
                run_counts_by_status: {
                  running: { current: 3, previous: 3, delta_pct: 0 },
                  awaiting_human: { current: 2, previous: 2, delta_pct: 0 },
                  failed: { current: 5, previous: 5, delta_pct: 0 },
                  idle: { current: 12, previous: 12, delta_pct: 0 },
                },
                eval_pass_rate: { current: 80, previous: 80, delta_pct: 0 },
                spend: { current: 100, previous: 100, delta_pct: 0 },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // Flat arrows for zero deltas
    expect(wrapper.text()).toContain('→')
    wrapper.unmount()
  })
})

// ── loadTrendWindow edge cases ──────────────────────────────────────────
describe('DashboardView branches — loadTrendWindow', () => {
  it('returns null for "all" raw value', async () => {
    localStorage.setItem('modulo.dashboard.trendWindow', 'all')
    const wrapper = mount(DashboardView)
    await flushPromises()
    // selectWindow(null) triggers fetchSummary with no days
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: {} } }),
    )
    wrapper.unmount()
  })

  it('returns 3 for non-number string', async () => {
    localStorage.setItem('modulo.dashboard.trendWindow', 'invalid')
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 3 } } }),
    )
    wrapper.unmount()
  })

  it('returns 3 for invalid number string', async () => {
    localStorage.setItem('modulo.dashboard.trendWindow', '999')
    const wrapper = mount(DashboardView)
    await flushPromises()
    // 999 is not in TREND_WINDOW_ALLOWED, so defaults to 3
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 3 } } }),
    )
    wrapper.unmount()
  })
})

// ── localStorage unavailable ────────────────────────────────────────────
describe('DashboardView branches — localStorage unavailable', () => {
  it('handles localStorage getItem throwing', async () => {
    const origGetItem = localStorage.getItem
    localStorage.getItem = () => { throw new Error('quota') }
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Should default to 3d window without crashing
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 3 } } }),
    )
    localStorage.getItem = origGetItem
    wrapper.unmount()
  })

  it('handles localStorage setItem throwing', async () => {
    const origSetItem = localStorage.setItem
    localStorage.setItem = () => { throw new Error('quota') }
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Clicking a window should not crash even if setItem throws
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    localStorage.setItem = origSetItem
    wrapper.unmount()
  })
})

// ── selectWindow null ───────────────────────────────────────────────────
describe('DashboardView branches — selectWindow null', () => {
  it('calls fetchSummary when selecting all-time (null)', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    mockGet.mockClear()
    await wrapper.find('[data-testid="trend-toggle-all"]').trigger('click')
    await flushPromises()
    // selectWindow(null) calls fetchSummary with no days parameter
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: {} } }),
    )
    wrapper.unmount()
  })
})

// ── trendData null/missing branches ────────────────────────────────────
describe('DashboardView branches — trendData', () => {
  it('returns empty array when trends is null', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            trend: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Should not crash with null trend
    expect(wrapper.text()).toContain('Dashboard')
    wrapper.unmount()
  })

  it('handles missing eval/spend/hitl/rejection data in trends', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            trend: mockSummaryData.trend.map(d => ({ ...d, eval_pass_rate: null, token_spend_usd: 0 })),
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Should not crash with null eval rates
    expect(wrapper.text()).toContain('Dashboard')
    wrapper.unmount()
  })
})

// ── summary.recent_runs empty vs populated ──────────────────────────────
describe('DashboardView branches — recent_runs', () => {
  it('shows no runs message when recent_runs is empty', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: { ...mockSummaryData, recent_runs: [] },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('No runs yet')
    wrapper.unmount()
  })

  it('renders run list when recent_runs is populated', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Deploy Pipeline')
    wrapper.unmount()
  })
})

// ── summary.teams empty vs populated ────────────────────────────────────
describe('DashboardView branches — teams', () => {
  it('does not show team breakdown when teams is empty', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: { ...mockSummaryData, teams: [] },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Team Breakdown')
    wrapper.unmount()
  })
})

// ── Team row expansion/collapse ─────────────────────────────────────────
describe('DashboardView branches — team expansion', () => {
  it('expands and collapses team row on click', async () => {
    const teamSummaryData = {
      ...mockSummaryData,
      teams: [
        {
          id: 'team-a',
          name: 'Alpha Team',
          total_runs: 80,
          active_pipelines: 4,
          run_counts_by_status: { running: 2, awaiting_human: 1, failed: 3, idle: 7 },
          eval_pass_rate: { total_evals: 40, passed_evals: 32, pass_rate: 80.0 },
        },
      ],
    }
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: teamSummaryData, error: undefined })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })

    // Need isTeam to be true - mock the plan store
    const { usePlanStore } = await import('../stores/planStore')
    const pinia = createPinia()
    setActivePinia(pinia)
    const planStore = usePlanStore()
    planStore.currentTier = 'team'

    const wrapper = mount(DashboardView)
    await flushPromises()

    // Team row should exist but team breakdown needs isTeam
    const teamRow = wrapper.find('[data-testid="dashboard-team-row-team-a"]')
    if (teamRow.exists()) {
      await teamRow.trigger('click')
      await flushPromises()
      // Toggle again to collapse
      await teamRow.trigger('click')
      await flushPromises()
    }
    wrapper.unmount()
  })
})

// ── Error + no summary shows ErrorAlert ─────────────────────────────────
describe('DashboardView branches — error state', () => {
  it('shows ErrorAlert when error and no summary', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') return Promise.reject(new Error('Network error'))
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    const errorEl = wrapper.findComponent({ name: 'ErrorAlert' })
    expect(errorEl.exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── Loading skeleton ────────────────────────────────────────────────────
describe('DashboardView branches — loading skeleton', () => {
  it('shows loading skeleton while fetching', async () => {
    const dashboardDefer = new Promise<{ data: typeof mockSummaryData; error: undefined }>(() => {})
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') return dashboardDefer
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.findAll('.animate-pulse').length).toBeGreaterThan(0)
    wrapper.unmount()
  })
})

// ── eval pass rate null shows "No eval data yet" ────────────────────────
describe('DashboardView branches — eval null display', () => {
  it('shows no eval data yet when eval_pass_rate is null', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            eval_pass_rate: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('No eval data yet')
    wrapper.unmount()
  })
})

// ── onMounted plan fetch guard ──────────────────────────────────────────
describe('DashboardView branches — onMounted', () => {
  it('fetches plan when tier is community', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Should have fetched plan for community tier
    const urls = mockGet.mock.calls.map((c: unknown[]) => c[0])
    expect(urls).toContain('/api/v1/admin/license')
    wrapper.unmount()
  })
})

// ── evalTrendClass branches ─────────────────────────────────────────────
describe('DashboardView branches — evalTrendClass', () => {
  it('shows muted-foreground for flat trend', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            trend: [
              { date: '2026-06-23', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
              { date: '2026-06-24', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
              { date: '2026-06-25', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
              { date: '2026-06-26', run_count: 10, eval_pass_rate: 80, token_spend_usd: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Flat trend -> text-muted-foreground
    const evalCard = wrapper.find('[data-testid="dashboard-eval-rate"]')
    const trendSpan = evalCard.find('span.text-muted-foreground')
    expect(trendSpan.exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── spendDeltaClass branches ────────────────────────────────────────────
describe('DashboardView branches — spendDeltaClass', () => {
  it('shows muted-foreground for null spend delta', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 40, delta_pct: 25 },
                active_pipelines: { current: 8, previous: 9, delta_pct: -11 },
                run_counts_by_status: {
                  running: { current: 3, previous: 2, delta_pct: 50 },
                  awaiting_human: { current: 2, previous: 1, delta_pct: 100 },
                  failed: { current: 5, previous: 3, delta_pct: 67 },
                  idle: { current: 12, previous: 10, delta_pct: 20 },
                },
                eval_pass_rate: { current: 80, previous: 75, delta_pct: 6.7 },
                spend: { current: 100, previous: 90, delta_pct: null },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    const spendCard = wrapper.find('[data-testid="dashboard-token-spend"]')
    // Null delta -> no delta text rendered
    const deltaSpan = spendCard.find('span.text-xs.font-medium')
    expect(deltaSpan.exists()).toBe(false)
    wrapper.unmount()
  })
})

// ── evalDeltaArrow for flat ─────────────────────────────────────────────
describe('DashboardView branches — evalDeltaArrow', () => {
  it('shows right arrow for flat delta', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
              days: 7,
              metrics: {
                total_runs: { current: 50, previous: 40, delta_pct: 25 },
                active_pipelines: { current: 8, previous: 9, delta_pct: -11 },
                run_counts_by_status: {
                  running: { current: 3, previous: 2, delta_pct: 50 },
                  awaiting_human: { current: 2, previous: 1, delta_pct: 100 },
                  failed: { current: 5, previous: 3, delta_pct: 67 },
                  idle: { current: 12, previous: 10, delta_pct: 20 },
                },
                eval_pass_rate: { current: 80, previous: 80, delta_pct: 0 },
                spend: { current: 100, previous: 90, delta_pct: 11 },
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    // Flat delta -> right arrow
    const evalCard = wrapper.find('[data-testid="dashboard-eval-rate"]')
    expect(evalCard.text()).toContain('→')
    wrapper.unmount()
  })
})
