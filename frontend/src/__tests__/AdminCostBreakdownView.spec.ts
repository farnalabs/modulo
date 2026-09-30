import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 250.0, total_runs: 10 },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 150.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({
          data: {
            buckets: [
              { date: '2026-09-01', count: 10, success_rate: 0.8 },
              { date: '2026-09-02', count: 5, success_rate: 1.0 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-1', anomaly_date: '2026-06-28', pipeline_id: null, amount: 150.0, baseline: 50.0, percent_above: 200.0, dismissed: false },
            { id: 'anomaly-2', anomaly_date: '2026-06-25', pipeline_id: null, amount: 90.0, baseline: 45.0, percent_above: 100.0, dismissed: true },
          ],
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: {
            license: { tier: 'team', has_license_key: true, is_valid: true },
            flags: [{ name: 'admin_cost_breakdown', description: '', tier: 'team', currently_active: true, depends_on: null }],
            would_activate: [],
          },
          error: undefined,
        })
      }
      if (path.startsWith('/api/v1/admin/costs/anomalies/dismiss/')) {
        return Promise.resolve({ data: null, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockImplementation((path: string) => {
      if (path.startsWith('/api/v1/admin/costs/anomalies/dismiss/')) {
        return Promise.resolve({ data: null, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminCostBreakdownView from '../views/AdminCostBreakdownView.vue'

describe('AdminCostBreakdownView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Cost Breakdown')
  })

  it('dismisses an anomaly via POST', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const dismissButton = wrapper.find('[data-testid="cost-anomaly-dismiss-anomaly-1"]')
    expect(dismissButton.exists()).toBe(true)
    await dismissButton.trigger('click')
    await nextTick()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/admin/costs/anomalies/dismiss/{anomaly_id}', {
      params: { path: { anomaly_id: 'anomaly-1' } },
    })
  })

  it('renders summary cards from org_total/org_run_count when items is empty', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [],
            org_total: '90.138792',
            org_run_count: 42,
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-total-spend"]').text()).toContain('90.14')
    expect(wrapper.find('[data-testid="cost-total-runs"]').text()).toContain('42')
  })

  it('calculates cost per successful run from analytics buckets', async () => {
    // Cost report: $400 total spend, 15 total runs
    // Analytics: bucket 1 has 10 runs * 80% success = 8 successful, bucket 2 has 5 * 100% = 5 successful
    // Total successful = 13, cost per successful run = 400 / 13 ≈ 30.77
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 250.0, total_runs: 10 },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 150.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({
          data: {
            buckets: [
              { date: '2026-09-01', count: 10, success_rate: 0.8 },
              { date: '2026-09-02', count: 5, success_rate: 1.0 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    // successfulRuns = round(10 * 0.8) + round(5 * 1.0) = 8 + 5 = 13
    // costPerSuccessfulRun = 400 / 13 ≈ 30.77
    expect(card.text()).toContain('30.77')

    const countEl = wrapper.find('[data-testid="cost-successful-runs-count"]')
    expect(countEl.exists()).toBe(true)
    expect(countEl.text()).toContain('13')
  })

  it('shows dash when zero successful runs (analytics returns all failures)', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({
          data: {
            buckets: [
              { date: '2026-09-01', count: 5, success_rate: 0.0 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    // Zero successful runs → dash, not Infinity/NaN
    expect(card.text()).toContain('—')

    const countEl = wrapper.find('[data-testid="cost-successful-runs-count"]')
    expect(countEl.exists()).toBe(true)
    expect(countEl.text()).toContain('No successful runs')
  })

  it('shows dash when analytics endpoint fails (graceful degradation)', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({
          data: null,
          error: { status: 403, message: 'Forbidden' },
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    // Analytics failed → graceful degradation → dash
    expect(card.text()).toContain('—')
  })

  it('shows an error alert when the anomalies request fails', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: { buckets: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: null, error: { status: 500, message: 'Internal Server Error' } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const alert = wrapper.find('[data-testid="cost-anomalies-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load anomalies')
  })

  it('renders the anomaly list and omits the plus sign for a non-positive percent_above', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: { buckets: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-flat', anomaly_date: '2026-06-20', pipeline_id: null, amount: 50.0, baseline: 50.0, percent_above: 0, dismissed: false },
          ],
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-table"]').exists()).toBe(true)
    const list = wrapper.find('[data-testid="cost-anomalies-list"]')
    expect(list.exists()).toBe(true)
    expect(list.text()).toContain('0% above')
  })

  it('renders the empty states when there are no team rows and no anomalies', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: { buckets: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-anomalies-empty"]').exists()).toBe(true)
  })

  it('renders refused/clamped annotations and falls back to 0 for a zero-run team', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 10.0, total_runs: 0, annotations: { refused_total_usd: 12.5, clamped_total_usd: null } },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 20.0, total_runs: 4, annotations: { refused_total_usd: null, clamped_total_usd: 3.5 } },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: { buckets: [{ date: '2026-09-01', count: 4, success_rate: null }] }, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const refused = wrapper.find('[data-testid="cost-annotation-refused"]')
    expect(refused.exists()).toBe(true)
    expect(refused.text()).toContain('12.50')
    expect(wrapper.find('[data-testid="cost-annotation-clamped"]').exists()).toBe(true)
    // A team with zero runs uses the 0 fallback rather than dividing by zero.
    expect(wrapper.find('[data-testid="cost-team-table"]').text()).toContain('0.00')
  })

  it('degrades gracefully when the analytics response omits buckets', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('—')
  })

  it('shows an error alert when the anomalies request throws', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: { buckets: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.reject(new Error('anomalies unavailable'))
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const alert = wrapper.find('[data-testid="cost-anomalies-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load anomalies')
  })

  it('shows an error alert when dismissing an anomaly fails', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/analytics/query') {
        return Promise.resolve({ data: { buckets: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-1', anomaly_date: '2026-06-28', pipeline_id: null, amount: 150.0, baseline: 50.0, percent_above: 200.0, dismissed: false },
          ],
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    ;(api.POST as any).mockRejectedValueOnce(new Error('dismiss failed'))

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const dismissButton = wrapper.find('[data-testid="cost-anomaly-dismiss-anomaly-1"]')
    expect(dismissButton.exists()).toBe(true)
    await dismissButton.trigger('click')
    await flushPromises()

    const alert = wrapper.find('[data-testid="cost-anomalies-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to dismiss anomaly')
  })
})
