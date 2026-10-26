import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'

const apiState = vi.hoisted(() => ({
  orgLimit: 100,
  ceiling: null as Record<string, unknown> | null,
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs/limits') {
        return Promise.resolve({
          data: {
            org_daily_limit_usd: apiState.orgLimit,
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
      if (path === '/api/v1/admin/costs/ceiling') {
        return Promise.resolve({ data: apiState.ceiling, error: undefined })
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
    apiState.orgLimit = 100
    apiState.ceiling = null
  })

  async function mountWithFeatures() {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({
      features: { admin_spend_limits: true, admin_cost_controls: true },
      currentTier: 'team',
    })
    const wrapper = mount(AdminSpendLimitsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

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
    expect(wrapper.find('header.page-header + .page-tabs-wrapper').exists()).toBe(true)
    expect(wrapper.findAll('header.page-header + .page-tabs-wrapper .page-tab')).toHaveLength(4)
  })

  it('confirms org-limit and ceiling saves and styles a positive remaining budget', async () => {
    apiState.ceiling = { max_run_cost: 5, spend_ceiling: 100, remaining_budget_usd: 42 }
    const wrapper = await mountWithFeatures()

    const orgSave = wrapper.find('[data-testid="admin-spend-limits-org-save"]')
    expect(orgSave.exists(), 'the org-limit save button must render').toBe(true)
    await orgSave.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Org limit updated.')

    const ceilingSave = wrapper.find('[data-testid="admin-spend-limits-ceiling-save"]')
    expect(ceilingSave.exists(), 'the ceiling save button must render').toBe(true)
    await ceilingSave.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Ceilings updated.')

    const remaining = wrapper
      .findAll('span')
      .find((s) => s.classes().includes('text-success-text') && s.text().includes('42'))
    expect(remaining).toBeDefined()
    wrapper.unmount()
  })

  it('styles a zero remaining budget with the destructive token', async () => {
    apiState.ceiling = { max_run_cost: 5, spend_ceiling: 100, remaining_budget_usd: 0 }
    const wrapper = await mountWithFeatures()

    const remaining = wrapper
      .findAll('span')
      .find((s) => s.classes().includes('text-destructive') && s.text().includes('0.00'))
    expect(remaining).toBeDefined()
    wrapper.unmount()
  })

  it('renders no budget class when the remaining budget is unknown', async () => {
    apiState.ceiling = { max_run_cost: 5, spend_ceiling: 100, remaining_budget_usd: null }
    const wrapper = await mountWithFeatures()

    expect(wrapper.text()).toContain('Remaining budget')
    const row = wrapper.findAll('div').find((d) => d.text().includes('Remaining budget'))!
    expect(row.text()).toContain('—')
    wrapper.unmount()
  })

  it('marks an org total above the daily limit as over budget', async () => {
    apiState.orgLimit = 10
    const wrapper = await mountWithFeatures()

    const overBudget = wrapper
      .findAll('span')
      .find((s) => s.classes().includes('text-destructive') && s.text().includes('42.50'))
    expect(overBudget).toBeDefined()
    wrapper.unmount()
  })
})
