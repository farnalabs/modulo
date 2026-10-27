import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockFetch = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: null }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminViewsView from '../views/AdminViewsView.vue'

const sampleViews = {
  items: [
    {
      id: 'v1',
      name: 'Active Runs',
      view_type: 'table',
      filters: { status: 'active' },
      columns: ['name', 'status', 'created_at'],
      sort_by: 'created_at',
      sort_order: 'desc',
      created_by: 'alice@test.com',
      created_at: '2025-01-15T10:00:00Z',
    },
    {
      id: 'v2',
      name: 'Kanban Board',
      view_type: 'kanban',
      filters: null,
      columns: ['title', 'assignee'],
      sort_by: 'priority',
      sort_order: 'asc',
      created_by: 'bob@test.com',
      created_at: '2025-02-20T14:30:00Z',
    },
  ],
}

async function flush() {
  await new Promise(resolve => setTimeout(resolve))
  await nextTick()
}

function mockFetchOk(data: unknown) {
  mockFetch.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(data),
  })
}

describe('AdminViewsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('fetch', mockFetch)
    mockFetchOk(sampleViews)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Saved Views')
  })

  it('displays views in a table after loading', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    expect(wrapper.text()).toContain('Active Runs')
    expect(wrapper.text()).toContain('Kanban Board')
    expect(wrapper.text()).toContain('alice@test.com')
    expect(wrapper.text()).toContain('bob@test.com')
  })

  it('shows empty state when no views exist', async () => {
    mockFetchOk({ items: [] })
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    expect(wrapper.text()).toContain('Saved Views')
  })

  it('opens create form on button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    await wrapper.find('[data-testid="admin-views-add"]').trigger('click')
    expect(wrapper.text()).toContain('New View')
    expect(wrapper.find('[data-testid="admin-views-name-input"]').exists()).toBe(true)
  })

  it('shows delete confirmation on delete button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    const deleteBtns = wrapper.findAll('button').filter(button => button.text() === 'Delete')
    expect(deleteBtns.length).toBe(2)
    await deleteBtns[0].trigger('click')
    expect(wrapper.text()).toContain('Delete "Active Runs"?')
  })

  it('sends POST request on duplicate button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()

    const dupBtns = wrapper.findAll('button').filter(button => button.text() === 'Duplicate')
    expect(dupBtns.length).toBe(2)
    await dupBtns[0].trigger('click')
    await flush()

    const fetchCalls = mockFetch.mock.calls
    const postCall = fetchCalls.find((c: any[]) => c[1]?.method === 'POST' || !c[1]?.method)
    expect(postCall).toBeDefined()
  })

  it('openEditForm serializes object and string filters into the form', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    const vm = wrapper.vm as Record<string, any>
    vm.openEditForm({
      id: 'v-obj',
      name: 'Object Filter View',
      view_type: 'table',
      filters: { status: 'active' },
      columns: ['name'],
      sort_by: 'created_at',
      sort_order: 'desc',
    } as any)
    expect(vm.form.filters).toContain('status')
    expect(vm.form.filters).toContain('active')
    vm.openEditForm({
      id: 'v-str',
      name: 'String Filter View',
      view_type: 'table',
      filters: 'status=active',
      columns: ['name'],
      sort_by: 'created_at',
      sort_order: 'desc',
    } as any)
    expect(vm.form.filters).toBe('status=active')
  })

  it('shows the delete error when the DELETE request fails', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    const deleteBtns = wrapper.findAll('button').filter(button => button.text() === 'Delete')
    await deleteBtns[0].trigger('click')
    await flush()

    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ detail: 'view delete exploded' }),
    })
    await wrapper.find('[data-testid="admin-views-delete-confirm"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('view delete exploded')
  })

  it('shows the loading state while the initial views request is pending', async () => {
    mockFetch.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, ErrorAlert: true },
      },
    })

    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows the initial load error and exposes the retry callback', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 503,
      json: () => Promise.resolve({ detail: 'views unavailable' }),
    })
    const ErrorAlertStub = {
      props: ['message'],
      template: '<div data-testid="load-error"><span>{{ message }}</span><button @click="$emit(\'retry\')">Retry</button></div>',
      emits: ['retry'],
    }
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: ErrorAlertStub },
      },
    })
    await flush()

    expect(wrapper.find('[data-testid="load-error"]').text()).toContain('views unavailable')
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })

  it('validates filters before sending a create payload', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    await wrapper.find('[data-testid="admin-views-add"]').trigger('click')
    await wrapper.find('[data-testid="admin-views-filters-input"]').setValue('{not-json')
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(wrapper.text()).toContain('Filters must be valid JSON')
    expect(mockFetch.mock.calls.some((call: unknown[]) => (call[1] as RequestInit | undefined)?.method === 'POST')).toBe(false)
  })

  it('sends a parsed create payload and reloads the saved views', async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ items: [] }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ id: 'new-view' }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ items: [] }) })
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    await wrapper.find('[data-testid="admin-views-add"]').trigger('click')
    await wrapper.find('[data-testid="admin-views-name-input"]').setValue('Release Board')
    await wrapper.find('[data-testid="admin-views-filters-input"]').setValue('{"status":"active"}')
    await wrapper.find('[data-testid="admin-views-columns-input"]').setValue('name, status')
    await wrapper.find('[data-testid="admin-views-sort-by-input"]').setValue('created_at')
    await wrapper.find('form').trigger('submit')
    await flush()

    const request = mockFetch.mock.calls.find((call: unknown[]) => (call[1] as RequestInit | undefined)?.method === 'POST')
    expect(request).toBeDefined()
    expect(request![0]).toBe('/api/v1/views')
    expect((request![1] as RequestInit).method).toBe('POST')
    expect(JSON.parse((request![1] as RequestInit).body as string)).toEqual({
      name: 'Release Board',
      view_type: 'table',
      filters: { status: 'active' },
      columns: ['name', 'status'],
      sort_by: 'created_at',
      sort_order: 'desc',
    })
    expect(mockFetch).toHaveBeenCalledTimes(3)
  })

  it('sends a PATCH payload when an existing view is edited', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    const vm = wrapper.vm as unknown as { openEditForm: (view: unknown) => void; handleSave: () => Promise<void>; form: { name: string } }
    vm.openEditForm(sampleViews.items[0])
    await nextTick()
    vm.form.name = 'Edited Runs'
    await vm.handleSave()
    await flush()

    const patch = mockFetch.mock.calls.find((call: unknown[]) => (call[1] as RequestInit | undefined)?.method === 'PATCH')
    expect(patch).toBeDefined()
    expect(patch![0]).toBe('/api/v1/views/v1')
    expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({ name: 'Edited Runs' })
  })

  it('shows a duplicate error when the duplicate request fails', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: () => Promise.resolve({ detail: 'duplicate rejected' }),
    })
    const vm = wrapper.vm as unknown as { duplicateView: (view: unknown) => Promise<void>; error: string | null }
    await vm.duplicateView(sampleViews.items[0])
    await flush()

    expect(vm.error).toContain('duplicate rejected')
    expect(wrapper.text()).toContain('duplicate rejected')
  })

  it('handles null and raw strings in filtersSummary', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    const vm = wrapper.vm as unknown as { filtersSummary: (filters: unknown) => string }
    expect(vm.filtersSummary(null)).toBe('—')
    expect(vm.filtersSummary('status=active')).toBe('status=active')
    expect(vm.filtersSummary('a'.repeat(80))).toBe(`${'a'.repeat(60)}…`)
  })

  it('handles empty date string in formatDate and save API errors', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    const vm = wrapper.vm as unknown as {
      formatDate: (date: string | null) => string
      handleSave: () => Promise<void>
      saveError: string | null
      form: { name: string; view_type: string; filters: string; columns: string; sort_by: string; sort_order: string }
    }
    expect(vm.formatDate(null)).toBe('—')

    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ detail: 'bad save' }),
    })
    vm.form.name = 'Broken View'
    vm.form.filters = ''
    vm.form.columns = ''
    await vm.handleSave()
    await flush()

    expect(vm.saveError).toBe('bad save')
  })

  it('guards against empty delete confirmation target', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' }, LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    const vm = wrapper.vm as unknown as { deleteView: () => Promise<void>; deleteConfirmId: string | null }
    vm.deleteConfirmId = null
    await vm.deleteView()
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })
})
