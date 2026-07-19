import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import TeamComparisonView from '../views/TeamComparisonView.vue'
import { api } from '../lib/api/client'

type MockApiResult = Promise<{ data: unknown; error: unknown }>

function mockApiGet(implementation: (url: string) => MockApiResult) {
  const mock = api.GET as unknown as {
    mockImplementation: (callback: (url: string) => MockApiResult) => void
  }
  mock.mockImplementation(implementation)
}

function createMockSummary(overrides = {}) {
  return {
    total_runs: 150,
    active_pipelines: 12,
    run_counts_by_status: { running: 3, awaiting_human: 2, failed: 1, idle: 144 },
    teams: [
      { id: 'team-1', name: 'Alpha', total_runs: 80, active_pipelines: 5, run_counts_by_status: { running: 2, awaiting_human: 1, failed: 0, idle: 77 }, eval_pass_rate: { total_evals: 40, passed_evals: 32, pass_rate: 80 } },
      { id: 'team-2', name: 'Beta', total_runs: 70, active_pipelines: 7, run_counts_by_status: { running: 1, awaiting_human: 1, failed: 1, idle: 67 }, eval_pass_rate: { total_evals: 30, passed_evals: 18, pass_rate: 60 } },
    ],
    eval_pass_rate: {
      overall_pass_rate: 71.4,
      total_evals: 70,
      passed_evals: 50,
      per_pipeline: {},
      per_team_pipeline: {
        'team-1': { 'pipe-1': { total_evals: 40, passed_evals: 32, pass_rate: 80 } },
        'team-2': { 'pipe-2': { total_evals: 30, passed_evals: 18, pass_rate: 60 } },
      },
    },
    trend: [],
    recent_runs: [],
    config_warnings: [],
    ...overrides,
  }
}

function createMockTeams(overrides = {}) {
  return {
    items: [
      { id: 'team-1', member_count: 5 },
      { id: 'team-2', member_count: 3 },
    ],
    total: 2,
    page: 1,
    page_size: 100,
    ...overrides,
  }
}

async function waitForAsync() {
  await flushPromises()
  await nextTick()
}

describe('TeamComparisonView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: createMockTeams(), error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Team Comparison')
  })

  it('shows error alert when dashboard summary API fails', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: null, error: { detail: 'Failed to load dashboard data' } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()
    expect(wrapper.text()).toContain('Failed to load dashboard data')
  })

  it('shows error alert when teams list API fails', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: null, error: { detail: 'Failed to load teams' } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()
    expect(wrapper.text()).toContain('Failed to load teams')
  })

  it('shows spinner during loading state', async () => {
    mockApiGet(() => new Promise(() => {}))
    const wrapper = mount(TeamComparisonView)
    await nextTick()
    const spinner = wrapper.findComponent({ name: 'LoadingSpinner' })
    expect(spinner.exists()).toBe(true)
  })

  it('shows empty state when no teams exist', async () => {
    mockApiGet((url: string) => {
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
            config_warnings: [],
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: { items: [], total: 0, page: 1, page_size: 100 }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()
    expect(wrapper.text()).toContain('No teams found')
  })

  it('renders team data in comparison table', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: createMockTeams(), error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()
    expect(wrapper.text()).toContain('Alpha')
    expect(wrapper.text()).toContain('Beta')
    expect(wrapper.text()).toContain('80%')
    expect(wrapper.text()).toContain('60%')
    expect(wrapper.text()).toContain('150')
    expect(wrapper.text()).toContain('12')
  })

  it('expands team drill-down on click and shows pipeline evals', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: createMockTeams(), error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [{ id: 'pipe-1', name: 'Alpha Pipeline' }] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()

    const row = wrapper.find('[data-testid="team-comparison-team-row-team-1"]')
    await row.trigger('click')
    await waitForAsync()

    expect(wrapper.text()).toContain('Alpha — Pipeline Eval Breakdown')
    expect(wrapper.text()).toContain('Alpha Pipeline')
  })

  it('collapses team drill-down on second click', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: createMockTeams(), error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [{ id: 'pipe-1', name: 'Alpha Pipeline' }] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()

    const row = wrapper.find('[data-testid="team-comparison-team-row-team-1"]')
    await row.trigger('click')
    await waitForAsync()
    expect(wrapper.text()).toContain('Alpha — Pipeline Eval Breakdown')

    await row.trigger('click')
    await waitForAsync()
    expect(wrapper.text()).not.toContain('Alpha — Pipeline Eval Breakdown')
  })

  it('falls back to shortId when pipeline names API fails', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: createMockTeams(), error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: null, error: { detail: 'Pipeline API error' } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()

    const row = wrapper.find('[data-testid="team-comparison-team-row-team-1"]')
    await row.trigger('click')
    await waitForAsync()

    expect(wrapper.text()).toContain('Alpha — Pipeline Eval Breakdown')
  })

  it('shows pipeline count with pluralization', async () => {
    mockApiGet((url: string) => {
      if (url === '/api/v1/dashboard/summary') {
        return Promise.resolve({ data: createMockSummary(), error: undefined })
      }
      if (url === '/api/v1/admin/teams') {
        return Promise.resolve({ data: createMockTeams(), error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: { items: [{ id: 'pipe-1', name: 'Alpha Pipeline' }, { id: 'pipe-2', name: 'Beta Pipeline' }] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(TeamComparisonView)
    await waitForAsync()

    const row = wrapper.find('[data-testid="team-comparison-team-row-team-1"]')
    await row.trigger('click')
    await waitForAsync()

    expect(wrapper.text()).toContain('1 pipeline')
  })
})
