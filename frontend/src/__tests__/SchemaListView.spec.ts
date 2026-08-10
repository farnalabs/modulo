import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => {
  const schemas = [
    {
      id: '1',
      organisation_id: 'org-1',
      name: 'Active Schema',
      description: 'An active schema',
      abstract_name: null,
      folder_id: null,
      created_by: 'user-1',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      deprecated: false,
      deprecated_at: null,
    },
    {
      id: '2',
      organisation_id: 'org-1',
      name: 'Deprecated Schema',
      description: 'A deprecated schema',
      abstract_name: null,
      folder_id: 'folder-1',
      created_by: 'user-1',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      deprecated: true,
      deprecated_at: '2026-06-01T00:00:00Z',
    },
  ]
  return {
    api: {
      GET: vi.fn().mockResolvedValue({
        data: { items: schemas, total: 2, page: 1, page_size: 100 },
        error: undefined,
      }),
      PATCH: vi.fn().mockResolvedValue({
        data: { ...schemas[0], deprecated: true, deprecated_at: '2026-06-29T00:00:00Z' },
        error: undefined,
      }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

const mockPush = vi.fn()
vi.mock('vue-router', async () => {
  const actual = await vi.importActual('vue-router')
  return {
    ...actual as any,
    useRouter: () => ({ push: mockPush }),
    useRoute: () => ({ path: '/schemas' }),
  }
})

vi.mock('@/composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ]),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn().mockResolvedValue(undefined),
    delete: vi.fn(),
  }),
}))

import SchemaListView from '../views/SchemaListView.vue'

// The reka-ui DropdownMenu popper cannot position itself in jsdom (throws
// "Cannot read properties of null (reading 'insertBefore')"). Stub the menu
// family so items render inline while still exercising the view's handlers.
const MenuStubs = {
  DropdownMenu: { template: '<div class="ddm-root"><slot /></div>' },
  DropdownMenuTrigger: { template: '<div class="ddm-trigger"><slot /></div>' },
  DropdownMenuContent: { template: '<div class="ddm-content"><slot /></div>' },
  DropdownMenuItem: {
    template: '<div class="ddm-item" @click="$emit(\'click\', $event)"><slot /></div>',
  },
}

function mountView() {
  return mount(SchemaListView, {
    global: {
      stubs: {
        LoadingSpinner: true,
        ErrorAlert: true,
        ...MenuStubs,
      },
    },
  })
}

describe('SchemaListView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPush.mockClear()
    document.body.innerHTML = ''
  })

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schemas')
  })

  it('displays active and deprecated badges', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('Active Schema')
    expect(wrapper.text()).toContain('Deprecated Schema')
    expect(wrapper.text()).toContain('Active')
    expect(wrapper.text()).toContain('Deprecated')
  })

  it('renders the folder tree sidebar', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="folder-tree"]').exists()).toBe(true)
  })

  it('shows deprecate action only for active schemas', async () => {
    const wrapper = mountView()
    await flushPromises()

    const deprecateItems = wrapper.findAll('[data-testid="schema-deprecate"]')
    expect(deprecateItems.length).toBe(1)

    // Every row offers a View / Edit affordance
    const viewEditItems = wrapper.findAll('[data-testid="schema-view-edit"]')
    expect(viewEditItems.length).toBe(2)
  })

  it('opens, confirms, and cancels deprecation via the confirmation dialog', async () => {
    const wrapper = mountView()
    await flushPromises()

    const deprecateItem = wrapper.find('[data-testid="schema-deprecate"]')
    await deprecateItem.trigger('click')
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).toContain('Deprecate "Active Schema"?')

    // Cancel deprecation
    const cancelBtn = document.body.querySelector('[data-testid="schema-deprecate-cancel"]') as HTMLElement | null
    expect(cancelBtn).not.toBeNull()
    cancelBtn!.click()
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).not.toContain('Deprecate "Active Schema"?')
    expect(wrapper.text()).toContain('Active')

    // Re-open and confirm deprecation
    await wrapper.find('[data-testid="schema-deprecate"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).toContain('Deprecate "Active Schema"?')

    const confirmBtn = document.body.querySelector('[data-testid="schema-deprecate-confirm"]') as HTMLElement | null
    expect(confirmBtn).not.toBeNull()
    confirmBtn!.click()
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).not.toContain('Deprecate "Active Schema"?')
    expect(wrapper.text()).toContain('Deprecated')
  })

  it('navigates to the schema editor on row click', async () => {
    const wrapper = mountView()
    await flushPromises()

    const row = wrapper.find('[data-testid="schema-row-1"]')
    await row.trigger('click')
    await nextTick()
    expect(mockPush).toHaveBeenCalledWith({ name: 'schema-editor', params: { id: '1' } })
  })

  it('does not navigate when the action menu trigger is clicked', async () => {
    const wrapper = mountView()
    await flushPromises()

    const trigger = wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]')
    await trigger.trigger('click')
    await nextTick()
    expect(mockPush).not.toHaveBeenCalled()
  })
})
