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
