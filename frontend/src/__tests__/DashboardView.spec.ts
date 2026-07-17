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

  it('renders run activity trend section', async () => {
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

  it('shows trend duration buttons', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    const buttons = wrapper.findAll('button')
    const trendButtons = buttons.filter(b => ['7d', '30d', '90d'].includes(b.text()))
    expect(trendButtons.length).toBe(3)
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
    expect(wrapper.text()).toContain('No data yet')
    expect(wrapper.text()).toContain('Run a Pipeline')
  })

  it('shows no eval data for null pass rate', async () => {
    setupEmptyMocks()
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('No eval data yet')
  })
})
