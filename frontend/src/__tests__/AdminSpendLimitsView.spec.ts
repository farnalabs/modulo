import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs/limits') {
        return Promise.resolve({
          data: {
            org_daily_limit_usd: 100.0,
            teams: [
              { id: 'team-1', name: 'Alpha', daily_limit_usd: 50.0 },
              { id: 'team-2', name: 'Beta', daily_limit_usd: null },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 25.0, total_runs: 3, components: [{ name: 'llm_tokens', amount_usd: '25.000000' }], annotations: { refused_total_usd: null, clamped_total_usd: null } },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 17.5, total_runs: 2, components: [], annotations: { refused_total_usd: 1.25, clamped_total_usd: null } },
            ],
            org_total: '42.500000',
            legacy_total: '0.000000',
            org_unassigned_components: '0.000000',
            has_more: false,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: {
            license: { tier: 'team', has_license_key: true, is_valid: true },
            flags: [{ name: 'admin_spend_limits', description: '', tier: 'team', currently_active: true, depends_on: null }],
            would_activate: [],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminSpendLimitsView from '../views/AdminSpendLimitsView.vue'

describe('AdminSpendLimitsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_spend_limits: true } })

    const wrapper = mount(AdminSpendLimitsView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Spend Limits')
  })
})
