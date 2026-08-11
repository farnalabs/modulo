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
const patchMock = vi.hoisted(() => vi.fn())

vi.mock('vue-router', async () => {
  const actual = await vi.importActual('vue-router')
  return {
    ...actual as any,
    useRouter: () => ({ push: mockPush }),
    useRoute: () => ({ path: '/schemas' }),
  }
})

const getMock = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useApi', () => {
  getMock.mockResolvedValue([
    { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
  ])
  return {
    useApi: () => ({
      get: getMock,
      post: vi.fn(),
      put: vi.fn(),
      patch: patchMock.mockResolvedValue(undefined),
      delete: vi.fn(),
    }),
  }
})

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

  it('moves a schema to a folder via the actions menu', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="schema-move-folder"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).toContain('Move to Folder')

    const folderBtn = document.body.querySelector('[data-testid="schema-move-folder-folder-1"]') as HTMLElement | null
    expect(folderBtn).not.toBeNull()
    folderBtn!.click()
    await nextTick()

    const confirmBtn = document.body.querySelector('[data-testid="schema-move-confirm"]') as HTMLElement | null
    expect(confirmBtn).not.toBeNull()
    confirmBtn!.click()
    await flushPromises()
    await nextTick()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/schemas/1/folder', { folder_id: 'folder-1' })
    expect(document.body.textContent).not.toContain('Move to Folder')
  })

  it('keeps the mobile folder select reachable when the selected folder is empty', async () => {
    const { api } = await import('../lib/api/client')
    const getMock = api.GET as ReturnType<typeof vi.fn>
    const originalImpl = getMock.getMockImplementation()
    getMock.mockImplementation(() => Promise.resolve({
      data: { items: [], total: 0, page: 1, page_size: 100 },
      error: undefined,
    }))

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('No schemas found')

    // Mobile folder select + breadcrumb are hoisted out of the empty-state
    // branch, so a mobile user is never stranded in an empty folder.
    expect(wrapper.find('[data-testid="schema-list-mobile-folder-select"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('All Schemas')

    getMock.mockImplementation(originalImpl as ReturnType<typeof vi.fn>)
  })

  it('moves a schema back to no folder via the actions menu', async () => {
    const wrapper = mountView()
    await flushPromises()

    const moveItems = wrapper.findAll('[data-testid="schema-move-folder"]')
    expect(moveItems.length).toBe(2)
    await moveItems[1].trigger('click')
    await flushPromises()
    await nextTick()

    const noFolderBtn = document.body.querySelector('[data-testid="schema-move-nofolder"]') as HTMLElement | null
    expect(noFolderBtn).not.toBeNull()
    noFolderBtn!.click()
    await nextTick()

    const confirmBtn = document.body.querySelector('[data-testid="schema-move-confirm"]') as HTMLElement | null
    confirmBtn!.click()
    await flushPromises()
    await nextTick()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/schemas/2/folder', { folder_id: null })
    expect(document.body.textContent).not.toContain('Move to Folder')
  })

  it('unfiles a schema when dropped on the All Schemas root', async () => {
    const wrapper = mountView()
    await flushPromises()

    const root = wrapper.find('[data-testid="folder-tree-all-pipelines"]')
    expect(root.exists()).toBe(true)
    await root.trigger('drop', { dataTransfer: { getData: () => '2' } })
    await flushPromises()
    await nextTick()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/schemas/2/folder', { folder_id: null })
  })

  it('shows folders-specific error copy when folders fail to load', async () => {
    // First get() call is the FolderTree's own folder fetch; the second is
    // this view's loadFolders — reject that one to surface folderError.
    getMock.mockResolvedValueOnce([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ])
    getMock.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load folders')
  })
})
