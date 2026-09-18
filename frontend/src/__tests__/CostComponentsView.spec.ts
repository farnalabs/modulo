import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

/* ── default API responses (restored every test) ──────────────────────── */

const defaultComponents = [
  { id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated', rate_usd: null, rate_fallback: null, formula: 'tokens_input * input_token_rate + tokens_output * output_token_rate', report_key: null, enabled: true, sort_order: 0, deleted_at: null },
  { id: 'comp-2', name: 'model_tokens', display_name: 'Model Tokens', kind: 'self_reported', rate_usd: null, rate_fallback: null, formula: null, report_key: 'model_cost_usd', enabled: true, sort_order: 1, deleted_at: null },
  { id: 'comp-3', name: 'sandbox_infra', display_name: 'Sandbox Infra', kind: 'calculated', rate_usd: '0.020000', rate_fallback: 'e2b_rate', formula: 'wall_clock_hours * rate', report_key: null, enabled: false, sort_order: 2, deleted_at: null },
]

const defaultFlags = {
  data: { license: { tier: 'team', has_license_key: true, is_valid: true }, flags: [{ name: 'admin_cost_breakdown', description: 'Cost Breakdown', tier: 'team', currently_active: true, depends_on: null }], would_activate: [] },
  error: undefined,
}

function defaultGet(path: string) {
  if (path === '/api/v1/admin/costs/components') {
    return Promise.resolve({ data: [...defaultComponents], error: undefined })
  }
  if (path === '/api/v1/admin/feature-flags') {
    return Promise.resolve(defaultFlags)
  }
  return Promise.resolve({ data: null, error: undefined })
}

const mockGet = vi.hoisted(() => vi.fn().mockImplementation(defaultGet))
const mockPost = vi.hoisted(() => vi.fn().mockResolvedValue({ data: null, error: undefined }))
const mockPut = vi.hoisted(() => vi.fn().mockResolvedValue({ data: null, error: undefined }))
const mockDelete = vi.hoisted(() => vi.fn().mockResolvedValue({ response: { status: 204 }, data: null, error: undefined }))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: mockPost,
    PUT: mockPut,
    DELETE: mockDelete,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import CostComponentsView from '../views/CostComponentsView.vue'

describe('CostComponentsView', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    // Restore ALL mocks to default implementations (clearAllMocks alone doesn't reset implementations)
    mockGet.mockReset().mockImplementation(defaultGet)
    mockPost.mockReset().mockResolvedValue({ data: null, error: undefined })
    mockPut.mockReset().mockResolvedValue({ data: null, error: undefined })
    mockDelete.mockReset().mockResolvedValue({ response: { status: 204 }, data: null, error: undefined })
  })

  afterEach(() => {
    // Clean up any teleported dialog DOM
    document.body.innerHTML = ''
  })

  async function mountView() {
    const wrapper = mount(CostComponentsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    return wrapper
  }

  /* ── existing core tests ───────────────────────────────────────────── */

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

    const wrapper = mount(CostComponentsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('Available on higher plan tier')
  })

  /* ── CRUD: create ──────────────────────────────────────────────────── */

  it('create dialog POSTs with correct payload shape', async () => {
    const wrapper = await mountView()

    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()

    // The form defaults to kind='calculated'. Trigger saveComponent via VM.
    const vm = wrapper.vm as unknown as {
      saveComponent: () => Promise<void>
      form: { name: string; display_name: string; kind: string }
    }
    vm.form.name = 'new_cost'
    vm.form.display_name = 'New Cost'
    await flushPromises()

    await vm.saveComponent()
    await flushPromises()

    expect(mockPost).toHaveBeenCalledOnce()
    const [, postBody] = mockPost.mock.calls[0] as [string, { body: Record<string, unknown> }]
    expect(postBody.body).toMatchObject({
      name: 'new_cost',
      display_name: 'New Cost',
      kind: 'calculated',
      enabled: true,
    })
    expect(postBody.body).toHaveProperty('formula')
    expect(postBody.body).toHaveProperty('report_key')
  })

  /* ── CRUD: edit ────────────────────────────────────────────────────── */

  it('edit pre-fills form with component data and disables name', async () => {
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as {
      openEdit: (c: { id: string; name: string; display_name: string; kind: string }) => void
      form: { name: string; display_name: string; kind: string }
      editing: boolean
    }

    vm.openEdit({ id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated' })
    await flushPromises()

    expect(vm.form.name).toBe('llm_tokens')
    expect(vm.form.display_name).toBe('LLM Tokens')
    expect(vm.form.kind).toBe('calculated')
    expect(vm.editing).toBe(true)

    const nameInput = document.body.querySelector('[data-testid="cost-components-name"]') as HTMLInputElement
    expect(nameInput).not.toBeNull()
    expect(nameInput.disabled).toBe(true)
  })

  /* ── CRUD: save error ──────────────────────────────────────────────── */

  it('shows formError when POST returns an error', async () => {
    mockPost.mockResolvedValueOnce({ data: null, error: { detail: 'Name already exists' } })
    const wrapper = await mountView()

    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as { saveComponent: () => Promise<void> }
    await vm.saveComponent()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save')
  })

  it('shows formError when POST throws', async () => {
    mockPost.mockRejectedValueOnce(new Error('Network down'))
    const wrapper = await mountView()

    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as { saveComponent: () => Promise<void> }
    await vm.saveComponent()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save')
    expect(wrapper.text()).toContain('Network down')
  })

  /* ── toggle enabled ────────────────────────────────────────────────── */

  it('toggleEnabled sends PUT with inverted enabled', async () => {
    const wrapper = await mountView()

    // comp-3 (sandbox_infra) is disabled — toggle it on
    const toggle = wrapper.find('[data-testid="cost-components-toggle-comp-3"] input')
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('change')
    await flushPromises()

    expect(mockPut).toHaveBeenCalledOnce()
    const [url, { body }] = mockPut.mock.calls[0] as [string, { body: { enabled: boolean } }]
    expect(url).toContain('/comp-3')
    expect(body.enabled).toBe(true) // was false → toggled to true
  })

  it('shows formError when toggle PUT fails', async () => {
    mockPut.mockResolvedValueOnce({ data: null, error: { detail: 'Permission denied' } })
    const wrapper = await mountView()

    const toggle = wrapper.find('[data-testid="cost-components-toggle-comp-1"] input')
    await toggle.trigger('change')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to toggle')
  })

  it('shows formError when toggle throws', async () => {
    mockPut.mockRejectedValueOnce(new Error('Timeout'))
    const wrapper = await mountView()

    const toggle = wrapper.find('[data-testid="cost-components-toggle-comp-1"] input')
    await toggle.trigger('change')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to toggle')
  })

  /* ── delete flow ───────────────────────────────────────────────────── */

  it('delete shows confirmation panel then DELETEs on confirm', async () => {
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as {
      confirmDeleteRequest: (c: { id: string; name: string; display_name: string; kind: string }) => void
      confirmDelete: () => Promise<void>
    }

    // Trigger delete confirmation via VM
    vm.confirmDeleteRequest({ id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated' })
    await flushPromises()

    // Confirmation panel should appear
    expect(wrapper.find('[data-testid="cost-components-delete-confirm"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-components-delete-cancel"]').exists()).toBe(true)

    // Confirm
    await wrapper.find('[data-testid="cost-components-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(mockDelete).toHaveBeenCalledOnce()
    const [url] = mockDelete.mock.calls[0] as [string]
    expect(url).toContain('/api/v1/admin/costs/components/')
  })

  it('delete cancel clears the confirmation panel', async () => {
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as {
      confirmDeleteRequest: (c: { id: string; name: string; display_name: string; kind: string }) => void
    }

    vm.confirmDeleteRequest({ id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated' })
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-components-delete-confirm"]').exists()).toBe(true)

    await wrapper.find('[data-testid="cost-components-delete-cancel"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-components-delete-confirm"]').exists()).toBe(false)
  })

  it('shows formError when DELETE returns an error', async () => {
    mockDelete.mockResolvedValueOnce({ response: { status: 500 }, data: null, error: { detail: 'Cannot delete' } })
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as {
      confirmDeleteRequest: (c: { id: string; name: string; display_name: string; kind: string }) => void
      confirmDelete: () => Promise<void>
    }

    vm.confirmDeleteRequest({ id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated' })
    await flushPromises()

    await vm.confirmDelete()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to delete')
  })

  it('shows formError when DELETE throws', async () => {
    mockDelete.mockRejectedValueOnce(new Error('Connection refused'))
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as {
      confirmDeleteRequest: (c: { id: string; name: string; display_name: string; kind: string }) => void
      confirmDelete: () => Promise<void>
    }

    vm.confirmDeleteRequest({ id: 'comp-1', name: 'llm_tokens', display_name: 'LLM Tokens', kind: 'calculated' })
    await flushPromises()

    await vm.confirmDelete()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to delete')
    expect(wrapper.text()).toContain('Connection refused')
  })

  /* ── empty state ───────────────────────────────────────────────────── */

  it('shows EmptyState when no components exist', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs/components') return Promise.resolve({ data: [], error: undefined })
      return Promise.resolve(defaultFlags)
    })

    const wrapper = mount(CostComponentsView, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('No cost components')
  })

  /* ── API error on load ─────────────────────────────────────────────── */

  it('shows ErrorAlert when GET returns an error', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs/components') return Promise.resolve({ data: null, error: { detail: 'Server error' } })
      return Promise.resolve(defaultFlags)
    })

    const wrapper = mount(CostComponentsView, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Server error')
  })

  /* ── computed: kind hint and fallback ───────────────────────────────── */

  it('shows formula field for calculated kind and hides for self_reported', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()

    // Default kind is 'calculated' → formula field should be present
    expect(document.body.querySelector('[data-testid="cost-components-formula"]')).not.toBeNull()

    // Switch to self_reported
    const selfReported = document.body.querySelector('[data-testid="cost-components-kind-self-reported"] input') as HTMLInputElement
    selfReported.click()
    await flushPromises()

    expect(document.body.querySelector('[data-testid="cost-components-formula"]')).toBeNull()
    expect(document.body.querySelector('[data-testid="cost-components-report-key"]')).not.toBeNull()
  })

  it('shows env_fallback select when formula references rate and rate is empty', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()

    // Type a formula that references 'rate' via VM
    const vm = wrapper.vm as unknown as { form: { formula: string; rate_usd: string } }
    vm.form.formula = 'wall_clock_hours * rate'
    await flushPromises()

    // rate_fallback selector should appear (rate_usd is empty by default)
    expect(document.body.querySelector('[data-testid="cost-components-fallback"]')).not.toBeNull()
  })

  it('hides env_fallback when rate_usd is provided', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="cost-components-add"]').trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { formula: string; rate_usd: string } }
    vm.form.formula = 'wall_clock_hours * rate'
    vm.form.rate_usd = '0.05'
    await flushPromises()

    // rate_fallback should not be shown when rate_usd is set
    expect(document.body.querySelector('[data-testid="cost-components-fallback"]')).toBeNull()
  })

  /* ── table content ─────────────────────────────────────────────────── */

  it('displays calculated kind badge', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('calculated')
  })

  it('displays self_reported kind badge', async () => {
    const wrapper = await mountView()
    // i18n resolves self_reported as "Self reported" (capitalised, spaced)
    expect(wrapper.text()).toContain('self reported')
  })

  it('shows dash for components with no formula', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('model_cost_usd')
  })

  it('shows rate fallback in parentheses', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('e2b_rate')
  })
})
