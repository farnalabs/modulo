import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'

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
            license: { tier: 'enterprise', has_license_key: true, is_valid: true },
            flags: [{ name: 'admin_cost_breakdown', description: '', tier: 'enterprise', currently_active: true, depends_on: null }],
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
})
