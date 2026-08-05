import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn().mockImplementation((path: string) => {
  if (path === '/api/v1/admin/costs/components') {
    return Promise.resolve({
      data: [
        { id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated', rate_usd: null, rate_fallback: null, formula: 'tokens_input * input_token_rate + tokens_output * output_token_rate', report_key: null, enabled: true, sort_order: 0, deleted_at: null },
        { id: 'comp-2', name: 'model_tokens', display_name: 'Model Tokens', kind: 'self_reported', rate_usd: null, rate_fallback: null, formula: null, report_key: 'model_cost_usd', enabled: true, sort_order: 1, deleted_at: null },
        { id: 'comp-3', name: 'sandbox_infra', display_name: 'Sandbox Infra', kind: 'calculated', rate_usd: '0.020000', rate_fallback: 'e2b_rate', formula: 'wall_clock_hours * rate', report_key: null, enabled: false, sort_order: 2, deleted_at: null },
      ],
      error: undefined,
    })
  }
  if (path === '/api/v1/admin/feature-flags') {
    return Promise.resolve({
      data: { license: { tier: 'team', has_license_key: true, is_valid: true }, flags: [{ name: 'admin_cost_breakdown', description: 'Cost Breakdown', tier: 'team', currently_active: true, depends_on: null }], would_activate: [] },
      error: undefined,
    })
  }
  return Promise.resolve({ data: null, error: undefined })
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204 }, data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import CostComponentsView from '../views/CostComponentsView.vue'

describe('CostComponentsView', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
  })

  async function mountView() {
    const wrapper = mount(CostComponentsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    return wrapper
  }

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Cost Components')
  })

  it('displays component rows with kind, rate and formula', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('LLM Tokens')
    expect(wrapper.text()).toContain('Model Tokens')
    expect(wrapper.text()).toContain('Sandbox Infra')
    expect(wrapper.find('[data-testid="cost-components-toggle-comp-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-components-toggle-comp-3"]').exists()).toBe(true)
  })

  it('opens the create dialog', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()
    // reka-ui Dialog teleports content to document.body.
    expect(document.body.querySelector('[data-testid="cost-components-name"]')).not.toBeNull()
    expect(document.body.querySelector('[data-testid="cost-components-kind-calculated"]')).not.toBeNull()
    expect(document.body.querySelector('[data-testid="cost-components-kind-self-reported"]')).not.toBeNull()
  })

  it('shows the report_key field for self_reported kind', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()
    const selfReported = document.body.querySelector('[data-testid="cost-components-kind-self-reported"] input') as HTMLInputElement | null
    expect(selfReported).not.toBeNull()
    selfReported!.click()
    await flushPromises()
    expect(document.body.querySelector('[data-testid="cost-components-report-key"]')).not.toBeNull()
  })

  it('shows locked state when feature is disabled', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: { license: { tier: 'community', has_license_key: false, is_valid: true }, flags: [{ name: 'admin_cost_breakdown', description: 'Cost Breakdown', tier: 'team', currently_active: false, depends_on: null }], would_activate: [] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    pinia = createPinia()
    setActivePinia(pinia)

    const wrapper = mount(CostComponentsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('Available on higher plan tier')
  })
})
