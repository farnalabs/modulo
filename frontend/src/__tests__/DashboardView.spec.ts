import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockSummaryData = {
  total_runs: 142,
  active_pipelines: 8,
  run_counts_by_status: { running: 3, awaiting_human: 2, failed: 5, idle: 12 },
  teams: [
    {
      id: 'team-a',
      name: 'Alpha Team',
      total_runs: 80,
      active_pipelines: 4,
      run_counts_by_status: { running: 2, awaiting_human: 1, failed: 3, idle: 7 },
      eval_pass_rate: { total_evals: 40, passed_evals: 32, pass_rate: 80.0 },
    },
    {
      id: 'team-b',
      name: 'Beta Team',
      total_runs: 62,
      active_pipelines: 4,
      run_counts_by_status: { running: 1, awaiting_human: 1, failed: 2, idle: 5 },
    },
  ],
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
    { id: 'run-2', pipeline_name: 'Test Suite', status: 'running', created_at: '2026-06-29T10:15:00Z', trigger_type: 'webhook' },
    { id: 'run-3', pipeline_name: 'Data Sync', status: 'failed', created_at: '2026-06-29T09:45:00Z', trigger_type: 'cron' },
    { id: 'run-4', pipeline_name: 'Code Review', status: 'awaiting_human', created_at: '2026-06-29T08:00:00Z', trigger_type: 'manual' },
    { id: 'run-5', pipeline_name: 'Backup Job', status: 'complete', created_at: '2026-06-28T23:00:00Z', trigger_type: 'cron' },
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
import StatCard from '../components/StatCard.vue'

function setupDefaultMocks() {
  mockGet.mockImplementation((url: string) => {
    if (url === '/api/v1/dashboard/summary') return Promise.resolve({ data: mockSummaryData, error: undefined })
    if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
    if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
}

function setupEmptyMocks() {
  mockGet.mockImplementation((url: string) => {
    if (url === '/api/v1/dashboard/summary') {
      return Promise.resolve({
        data: {
          total_runs: 0,
          active_pipelines: 0,
          run_counts_by_status: { running: 0, awaiting_human: 0, failed: 0, idle: 0 },
          teams: [],
          eval_pass_rate: null,
          trend: [],
          recent_runs: [],
        },
        error: undefined,
      })
    }
    if (url === '/api/v1/admin/feature-flags') return Promise.resolve({ data: mockFlagData, error: undefined })
    if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
}

describe('DashboardView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('renders the heading', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Dashboard')
  })

  it('renders summary stat cards when data loads', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('142')
    expect(wrapper.text()).toContain('Total Runs')
    expect(wrapper.text()).toContain('Pipelines')
    expect(wrapper.text()).toContain('Running')
    expect(wrapper.text()).toContain('Awaiting Human')
    expect(wrapper.text()).toContain('Failed')
    expect(wrapper.text()).toContain('Idle')
  })

  it('renders eval pass rate card', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Eval Pass Rate')
    expect(wrapper.text()).toContain('82.5%')
  })

  it('renders token spend card', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Token Spend')
    expect(wrapper.text()).toContain('100.25')
  })

  it('does not show team breakdown for non-enterprise plans', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Team Breakdown')
  })

  it('hides the run activity trend section when the dashboard_charts flag is off', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    // dashboard_charts is off by default (MVP) — the trend section must not render.
    expect(wrapper.text()).not.toContain('Run Activity')
  })

  it('renders the run activity trend section when the dashboard_charts flag is on', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: { ...mockFlagData, flags: [{ name: 'dashboard_charts', currently_active: true }] },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/license') return Promise.resolve({ data: mockLicenseData, error: undefined })
      if (url === '/api/v1/dashboard/summary') return Promise.resolve({ data: mockSummaryData, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Run Activity')
    expect(wrapper.text()).toContain('7d')
    expect(wrapper.text()).toContain('30d')
    expect(wrapper.text()).toContain('90d')
  })

  it('renders recent runs list', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Recent Runs')
    expect(wrapper.text()).toContain('Deploy Pipeline')
    expect(wrapper.text()).toContain('Test Suite')
    expect(wrapper.text()).toContain('Data Sync')
  })

  it('renders status badges for each run', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    const badges = wrapper.findAll('.rounded-full')
    const badgeTexts = badges.map(b => b.text())
    expect(badgeTexts).toContain('complete')
    expect(badgeTexts).toContain('running')
    expect(badgeTexts).toContain('failed')
    expect(badgeTexts).toContain('awaiting_human')
  })

  it('shows trigger type for each run', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('manual')
    expect(wrapper.text()).toContain('webhook')
    expect(wrapper.text()).toContain('cron')
  })

  it('shows the eval trend indicator as up when improving', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('Improving')
  })

  it('shows loading skeleton while dashboard data is fetching', async () => {
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
  })

  it('shows no runs message when recent_runs is empty', async () => {
    setupEmptyMocks()
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('No runs yet')
  })

  it('shows the rolling-window toggle with 6 values including All time', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    const toggles = wrapper.findAll('[data-testid^="trend-toggle-"]')
    expect(toggles).toHaveLength(6)
    expect(wrapper.findAll('[data-testid="trend-toggle-1"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="trend-toggle-3"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="trend-toggle-7"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="trend-toggle-30"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="trend-toggle-90"]').length).toBe(1)
    expect(wrapper.findAll('[data-testid="trend-toggle-all"]').length).toBe(1)
  })

  it('fetches a period-scoped summary when a window is selected', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 7 } } }),
    )
  })

  it('does not render the global loading skeleton when a window is selected', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-7"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.animate-pulse').length).toBe(0)
  })

  it('All-time option calls fetchSummary with no days', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-all"]').trigger('click')
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: {} } }),
    )
  })

  it('defaults to the 3d window when nothing is stored', async () => {
    localStorage.clear()
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 3 } } }),
    )
    expect(wrapper.find('[data-testid="trend-toggle-3"]').classes()).toContain('bg-primary')
  })

  it('persists the selected window to localStorage and restores it on mount', async () => {
    localStorage.clear()
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.find('[data-testid="trend-toggle-30"]').trigger('click')
    await flushPromises()
    expect(localStorage.getItem('modulo.dashboard.trendWindow')).toBe('30')

    const wrapper2 = mount(DashboardView)
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ params: { query: { days: 30 } } }),
    )
    expect(wrapper2.find('[data-testid="trend-toggle-30"]').classes()).toContain('bg-primary')
  })

  it('renders period-scoped stat values and trend arrows when a window is selected', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
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
    expect(wrapper.text()).toContain('25.0%') // total runs up
    expect(wrapper.text()).toContain('66.7%') // failed up
    expect(wrapper.text()).toContain('11.4%') // spend up
  })

  it('shows the no-prior-data fallback on stat cards whose delta_pct is null when a window is selected', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            period: {
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
    // running / awaiting_human / idle have delta_pct null -> muted em-dash
    // fallback with the "no prior period data" tooltip; the rest keep arrows.
    const fallbacks = wrapper.findAll('[data-testid="stat-no-baseline"]')
    expect(fallbacks.length).toBe(3)
    expect(fallbacks[0].attributes('title')).toBe('No prior period data')
  })

  it('does not render the no-prior-data fallback when no period data is present', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.find('[data-testid="stat-no-baseline"]').exists()).toBe(false)
  })

  it('shows error alert when fetch fails', async () => {
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
  })

  it('shows empty state messages for fresh orgs', async () => {
    setupEmptyMocks()
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('No runs yet')
    // The run-activity trend section (whose empty state was 'No data yet') is
    // hidden when dashboard_charts is off; the eval card empty state remains.
    expect(wrapper.text()).toContain('No eval data yet')
  })

  it('shows the eval trend indicator as declining when eval pass rates dip', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({
          data: {
            ...mockSummaryData,
            trend: mockSummaryData.trend.map((d, i) => ({
              ...d,
              eval_pass_rate: [90, 88, 85, 82, 80, 75, 70][i],
            })),
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
    expect(wrapper.text()).toContain('Declining')
    expect(wrapper.text()).not.toContain('Improving')
  })

  it('renders recent runs as links that navigate to the run detail page', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    const runLinks = wrapper.findAll('a[href^="/runs/"]')
    expect(runLinks.length).toBe(mockSummaryData.recent_runs.length)
    expect(runLinks[0].attributes('href')).toBe('/runs/run-1')
    expect(wrapper.findAll('a[href="/runs/run-4"]').length).toBe(1)
  })

  it('shows no eval data for null pass rate', async () => {
    setupEmptyMocks()
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('No eval data yet')
  })

  it('passes trend dates as labels and units to every dashboard sparkline', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/dashboard/summary') return Promise.resolve({ data: mockSummaryData, error: undefined })
      if (url === '/api/v1/dashboard/trends') {
        return Promise.resolve({
          data: {
            days: 7,
            run_counts: [
              { date: '2026-06-23', run_count: 18 },
              { date: '2026-06-24', run_count: 22 },
              { date: '2026-06-25', run_count: 15 },
            ],
            eval_pass_rates: [
              { date: '2026-06-23', total_evals: 10, passed_evals: 8, pass_rate: 80 },
              { date: '2026-06-24', total_evals: 10, passed_evals: 9, pass_rate: 90 },
              { date: '2026-06-25', total_evals: 10, passed_evals: 8, pass_rate: 80 },
            ],
            token_spend: [
              { date: '2026-06-23', total_spend_usd: 12.5 },
              { date: '2026-06-24', total_spend_usd: 15.2 },
              { date: '2026-06-25', total_spend_usd: 10.1 },
            ],
            hitl_volume: [],
            rejection_trend: [],
            correlation: [],
            feedback_volume: [],
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
    const sparklines = wrapper.findAllComponents({ name: 'SparklineChart' })
    // dashboard_charts is off by default (MVP): only the 2 always-on card
    // sparklines (eval pass rate, token spend) render. The 3 run-activity
    // sparklines are gated behind the flag.
    expect(sparklines.length).toBe(2)
    // Card sparklines carry units.
    expect(sparklines[0].props('unit')).toBe('%')
    expect(sparklines[1].props('unit')).toBe('$')
    // Card sparkline labels come from summary.trend dates.
    expect(sparklines[0].props('labels')).toEqual(mockSummaryData.trend.map(d => d.date))
  })

  it('renders the no-data placeholder inside sparklines when the trend is empty', async () => {
    setupEmptyMocks()
    const wrapper = mount(DashboardView)
    await flushPromises()
    // Token spend card sparkline gets an empty series → placeholder, not a line.
    const sparkline = wrapper.find('[data-testid="sparkline"]')
    expect(sparkline.exists()).toBe(true)
    expect(sparkline.find('polyline').exists()).toBe(false)
    expect(sparkline.find('.sparkline-no-data').exists()).toBe(true)
  })
})

describe('StatCard delta arrow', () => {
  it('renders no arrow when delta_pct is null (previous = 0)', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Running',
        value: 0,
        delta: { current: 0, previous: 0, delta_pct: null },
      },
    })
    expect(wrapper.text()).not.toContain('%')
    expect(wrapper.text()).not.toContain('▲')
    expect(wrapper.text()).not.toContain('▼')
  })

  it('renders a down arrow for a negative delta at 1dp', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Total Runs',
        value: 4,
        delta: { current: 4, previous: 8, delta_pct: -50 },
      },
    })
    expect(wrapper.text()).toContain('▼')
    expect(wrapper.text()).toContain('-4')
    expect(wrapper.text()).toContain('50.0%')
  })

  it('renders an up arrow for a positive delta at 1dp', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Total Runs',
        value: 8,
        delta: { current: 8, previous: 4, delta_pct: 100 },
      },
    })
    expect(wrapper.text()).toContain('▲')
    expect(wrapper.text()).toContain('+4')
    expect(wrapper.text()).toContain('100.0%')
  })

  it('renders a neutral arrow for a zero delta', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Total Runs',
        value: 8,
        delta: { current: 8, previous: 8, delta_pct: 0 },
      },
    })
    expect(wrapper.text()).toContain('→')
    expect(wrapper.text()).toContain('0')
    expect(wrapper.text()).toContain('0.0%')
  })

  it('inverted keeps the arrow raw but flags a positive delta as destructive', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Failed',
        value: 5,
        delta: { current: 5, previous: 3, delta_pct: 66.7 },
        inverted: true,
      },
    })
    // Arrow still points up (the count went up); only the color signals badness.
    expect(wrapper.text()).toContain('▲')
    expect(wrapper.text()).toContain('+2')
    expect(wrapper.text()).toContain('66.7%')
    const deltaSpan = wrapper.find('span.inline-flex')
    expect(deltaSpan.classes()).toContain('text-destructive')
    expect(deltaSpan.classes()).not.toContain('text-success')
  })

  it('inverted keeps the arrow raw but flags a negative delta as success', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Failed',
        value: 3,
        delta: { current: 3, previous: 5, delta_pct: -40 },
        inverted: true,
      },
    })
    // Arrow still points down (the count went down); only the color signals goodness.
    expect(wrapper.text()).toContain('▼')
    expect(wrapper.text()).toContain('-2')
    expect(wrapper.text()).toContain('40.0%')
    const deltaSpan = wrapper.find('span.inline-flex')
    expect(deltaSpan.classes()).toContain('text-success')
    expect(deltaSpan.classes()).not.toContain('text-destructive')
  })

  it('renders unchanged when no delta prop is provided', () => {
    const wrapper = mount(StatCard, {
      props: { label: 'Total Runs', value: 8 },
    })
    expect(wrapper.text()).not.toContain('%')
    expect(wrapper.text()).toContain('Total Runs')
    expect(wrapper.text()).toContain('8')
  })

  it('renders the muted no-baseline fallback when delta_pct is null and a label is provided', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Running',
        value: 0,
        delta: { current: 0, previous: 0, delta_pct: null },
        noBaselineLabel: 'No prior period data',
      },
    })
    const fallback = wrapper.find('[data-testid="stat-no-baseline"]')
    expect(fallback.exists()).toBe(true)
    // Flat arrow + absolute 0 — no relative % (there is no baseline to diff).
    expect(fallback.text()).toContain('→')
    expect(fallback.text()).toContain('0')
    expect(fallback.attributes('title')).toBe('No prior period data')
    expect(fallback.attributes('aria-label')).toBe('No prior period data')
    // No basis to judge direction → muted, never a sign-colored success/destructive.
    expect(fallback.classes()).toContain('text-muted-foreground')
    expect(wrapper.text()).not.toContain('%')
    expect(wrapper.text()).not.toContain('▲')
    expect(wrapper.text()).not.toContain('▼')
  })

  it('renders the absolute delta with an accessible name when there is no prior-period baseline', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Total Runs',
        value: 788,
        delta: { current: 788, previous: 0, delta_pct: null },
        noBaselineLabel: 'No prior period data',
      },
    })
    const fallback = wrapper.find('[data-testid="stat-no-baseline"]')
    expect(fallback.exists()).toBe(true)
    // Absolute change shown despite the % being a hyphen (no baseline).
    expect(fallback.text()).toContain('▲')
    expect(fallback.text()).toContain('+788')
    // A11y: the em-dash/arrow alone is not a name — role=img + aria-label.
    expect(fallback.attributes('role')).toBe('img')
    expect(fallback.attributes('aria-label')).toBe('No prior period data')
    expect(fallback.attributes('title')).toBe('No prior period data')
    expect(wrapper.text()).not.toContain('%')
  })

  it('does not render a no-baseline fallback when only one of current/previous is a number', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Total Runs',
        value: 788,
        delta: { current: 788, previous: null, delta_pct: null },
        noBaselineLabel: 'No prior period data',
      },
    })
    expect(wrapper.find('[data-testid="stat-no-baseline"]').exists()).toBe(false)
  })

  it('does not render the no-baseline fallback when noBaselineLabel is undefined', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Running',
        value: 0,
        delta: { current: 0, previous: 0, delta_pct: null },
      },
    })
    expect(wrapper.find('[data-testid="stat-no-baseline"]').exists()).toBe(false)
  })
})
