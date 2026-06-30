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
            org_total_usd: 42.5,
            teams: [
              { team_id: 'team-1', team_name: 'Alpha', cost_usd: 25.0, limit_usd: 50.0 },
              { team_id: 'team-2', team_name: 'Beta', cost_usd: 17.5, limit_usd: null },
            ],
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
