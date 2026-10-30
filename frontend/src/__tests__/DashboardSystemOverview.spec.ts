import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({ api: { GET: mockGet } }))

import DashboardSystemOverview from '../components/dashboard/DashboardSystemOverview.vue'

const map = { id: 'map-1', name: 'Delivery System', default_map: true, published_revision_id: 'rev-1' }
const overview = {
  system_map_id: 'map-1', revision_id: 'rev-1', revision_number: 2, generated_at: '2026-10-30T00:00:00Z',
  nodes: [{ node_key: 'build', node_type: 'pipeline', display_name: 'Build', health: 'unknown', queue_depth: null, throughput_per_min: null, failure_rate: null, telemetry_coverage: null }],
  loop_configs: [{ node_key: 'build', target_signal: 'failure', observation_window_seconds: 60, action_pipeline_id: 'p', budget_limit_usd: '1', cooldown_seconds: 30, outcome_metric: 'success', cooldown_active: false }],
  bottlenecks: [{ node_key: 'build', node_type: 'pipeline', display_name: 'Build', kind: 'unobserved', detail: 'No telemetry', severity: 'medium' }],
}

describe('DashboardSystemOverview', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
  })

  it('loads the default published map and renders unknown metrics honestly', async () => {
    mockGet.mockImplementation((url: string) => url === '/api/v1/system-maps'
      ? Promise.resolve({ data: { items: [map], total: 1, page: 1, page_size: 100 } })
      : Promise.resolve({ data: overview }))
    const wrapper = mount(DashboardSystemOverview)
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps', expect.anything())
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/overview', { params: { path: { map_id: 'map-1' } } })
    expect(wrapper.text()).toContain('Published revision 2')
    expect(wrapper.text()).toContain('Unknown')
    expect(wrapper.text()).toContain('Unobserved')
    expect(wrapper.get('[data-testid="dashboard-system-discover"]').attributes('disabled')).toBeDefined()
  })

  it('renders the honest empty state without an overview request when there are no maps', async () => {
    mockGet.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 100 } })
    const wrapper = mount(DashboardSystemOverview)
    await flushPromises()
    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('No system maps yet')
  })
})

describe('DashboardSystemOverview measured state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
  })

  it('renders measured metrics, healthy/critical health and active cooldowns', async () => {
    const richOverview = {
      ...overview,
      nodes: [
        {
          node_key: 'build', node_type: 'pipeline', display_name: 'Build', health: 'healthy',
          queue_depth: 3, throughput_per_min: 12, failure_rate: 0.123, telemetry_coverage: 0.5,
        },
        {
          node_key: 'deploy', node_type: 'pipeline', display_name: 'Deploy', health: 'critical',
          queue_depth: 0, throughput_per_min: 0, failure_rate: 1, telemetry_coverage: 1,
        },
      ],
      loop_configs: [{ ...overview.loop_configs[0], cooldown_active: true }],
      bottlenecks: [],
    }
    mockGet.mockImplementation((url: string) => url === '/api/v1/system-maps'
      ? Promise.resolve({ data: { items: [map], total: 1, page: 1, page_size: 100 } })
      : Promise.resolve({ data: richOverview }))
    const wrapper = mount(DashboardSystemOverview)
    await flushPromises()

    expect(wrapper.text()).toContain('12.3%')
    expect(wrapper.text()).toContain('50.0%')
    expect(wrapper.text()).toContain('100.0%')
    expect(wrapper.text()).toContain('healthy')
    expect(wrapper.text()).toContain('critical')
    expect(wrapper.text()).toContain('Cooldown active')
    expect(wrapper.find('[data-testid="dashboard-system-bottlenecks"]').exists()).toBe(false)
  })

  it('renders the error state when loading fails', async () => {
    mockGet.mockResolvedValue({ error: new Error('list down') })
    const wrapper = mount(DashboardSystemOverview)
    await flushPromises()

    expect(wrapper.text()).toContain('list down')
  })

  it('renders the unpublished state when no map has a published revision', async () => {
    mockGet.mockResolvedValue({
      data: { items: [{ ...map, published_revision_id: null }], total: 1, page: 1, page_size: 100 },
    })
    const wrapper = mount(DashboardSystemOverview)
    await flushPromises()

    expect(wrapper.find('[data-testid="dashboard-system-unpublished"]').exists()).toBe(true)
    expect(mockGet).toHaveBeenCalledTimes(1)
  })
})
