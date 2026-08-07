import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 20, next_cursor: null, has_more: false },
}

vi.mock('../lib/api/client', () => {
  const mockGet = vi.fn((url: string) => {
    if (url === '/api/v1/runs') {
      return Promise.resolve({ data: mockResponses['/api/v1/runs'] ?? mockResponses.default, error: undefined })
    }
    return Promise.resolve({ data: mockResponses.default, error: undefined })
  })
  return {
    api: {
      GET: mockGet,
      PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

import RunsListView from '../views/RunsListView.vue'

const baseRun = {
  run_id: 'run1',
  pipeline_id: 'p1',
  pipeline_name: 'Test Pipeline',
  status: 'complete',
  trigger_type: 'manual',
  run_number: 1,
  created_at: '2026-01-01T00:00:00Z',
  started_at: '2026-01-01T00:00:00Z',
  completed_at: '2026-01-01T00:02:14Z',
  error_code: null,
  total_cost_usd: 0.5,
  account_id: null,
}

function listWith(items: unknown[]) {
  return { items, total: items.length, page: 1, page_size: 20, next_cursor: null, has_more: false }
}

function mountView() {
  return mount(RunsListView, {
    global: {
      stubs: { ErrorAlert: true },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockResponses['/api/v1/runs'] = listWith([])
})

describe('RunsListView', () => {
  it('renders without crashing', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
  })

  it('renders empty state when no runs exist', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No runs found')
  })

  it('shows Start, End and Duration columns instead of Created / Last Run', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const text = wrapper.text()
    expect(text).toContain('Start')
    expect(text).toContain('End')
    expect(text).toContain('Duration')
    expect(text).not.toContain('Last Run')
    expect(text).not.toContain('Created')
  })

  it('renders duration formatted from start and end timestamps', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('2m 14s')
  })

  it('renders multi-hour durations with padded minutes', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, started_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T01:02:03Z' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('1h 02m')
  })

  it('shows a dash for duration when the run is still in progress', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, completed_at: null }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('—')
  })

  it('shows aggregate cost with a (+child) marker when child runs exist', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, child_runs_cost_usd: '0.25', aggregate_cost_usd: '0.75' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const aggregateCell = wrapper.find('[data-testid="runs-list-aggregate-cost"]')
    expect(aggregateCell.exists()).toBe(true)
    expect(aggregateCell.text()).toContain('0.7500')
    expect(wrapper.text()).toContain('(+child)')
  })

  it('shows own cost when aggregate equals own cost (no children)', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, child_runs_cost_usd: '0.000000', aggregate_cost_usd: '0.5' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-aggregate-cost"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('0.5000')
    expect(wrapper.text()).not.toContain('(+child)')
  })

  it('falls back to own cost when rollup fields are absent', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-aggregate-cost"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('0.5000')
    expect(wrapper.text()).not.toContain('NaN')
    expect(wrapper.text()).not.toContain('(+child)')
  })
})
