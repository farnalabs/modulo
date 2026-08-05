import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn().mockImplementation((path: string) => {
  if (path === '/api/v1/admin/costs') {
    return Promise.resolve({
      data: {
        period: 'month',
        group_by: 'team',
        items: [
          { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 2500.0, total_runs: 12, components: [{ name: 'llm_tokens', amount_usd: '2000.000000' }, { name: 'sandbox_infra', amount_usd: '500.000000' }], annotations: { refused_total_usd: null, clamped_total_usd: null } },
          { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 1750.0, total_runs: 10, components: [], annotations: { refused_total_usd: 12.5, clamped_total_usd: 80.0 } },
        ],
        org_total: '4250.000000',
        legacy_total: '0.000000',
        org_unassigned_components: '0.000000',
        has_more: false,
      },
      error: undefined,
    })
  }
  if (path === '/api/v1/admin/costs/limits') {
    return Promise.resolve({
      data: { org_daily_spend_limit: 10000.0, team_limits: [{ team_id: 'team-1', team_name: 'Alpha', daily_spend_limit: 5000.0 }, { team_id: 'team-2', team_name: 'Beta', daily_spend_limit: null }] },
      error: undefined,
    })
  }
  if (path === '/api/v1/admin/costs/controls') {
    return Promise.resolve({
      data: { budget: 10000, currency: 'USD', billingPeriod: 'monthly', alertThresholds: [50, 75, 90], circuitBreakerEnabled: false },
      error: undefined,
    })
  }
  if (path === '/api/v1/admin/feature-flags') {
    return Promise.resolve({
      data: { license: { tier: 'team', has_license_key: true, is_valid: true }, flags: [{ name: 'admin_cost_controls', description: 'Cost Controls', tier: 'team', currently_active: true, depends_on: null }], would_activate: [] },
      error: undefined,
    })
  }
  return Promise.resolve({ data: null, error: undefined })
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminCostControlsView from '../views/AdminCostControlsView.vue'

describe('AdminCostControlsView', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
  })

  async function mountView() {
    const wrapper = mount(AdminCostControlsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    return wrapper
  }

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Cost Controls')
  })

  it('displays budget overview with spend and remaining', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="cc-total-spend"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-budget"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-remaining"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-progress-bar"]').exists()).toBe(true)
  })

  it('displays team budget rows', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="cc-team-budget-team-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-team-budget-team-2"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-team-save-team-1"]').exists()).toBe(true)
  })

  it('displays alert threshold checkboxes', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="cc-threshold-50"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-threshold-75"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-threshold-90"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-threshold-100"]').exists()).toBe(true)
  })

  it('displays circuit breaker toggle', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="cc-circuit-breaker"]').exists()).toBe(true)
  })

  it('displays currency and billing period selectors', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="cc-currency"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-billing-period"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-budget-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cc-budget-save"]').exists()).toBe(true)
  })

  it('shows locked state when feature is disabled', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: { license: { tier: 'community', has_license_key: false, is_valid: true }, flags: [{ name: 'admin_cost_controls', description: 'Cost Controls', tier: 'team', currently_active: false, depends_on: null }], would_activate: [] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    pinia = createPinia()
    setActivePinia(pinia)

    const wrapper = mount(AdminCostControlsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('Available on higher plan tier')
  })
})
