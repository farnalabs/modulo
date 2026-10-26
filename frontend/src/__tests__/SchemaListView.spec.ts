import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

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
import FolderTree from '../components/pipelines/FolderTree.vue'

// ErrorAlert is captured (not stubbed blind) so the message prop and the
// retry-on-failure flows are assertable (same pattern as AdminConnectorsView).
const capturingErrorAlert = {
  props: ['message', 'onRetry', 'onDismiss', 'dismissLabel'],
  emits: ['on-dismiss'],
  template:
    '<div data-testid="error-alert"><span data-testid="error-alert-message">{{ message }}</span>' +
    '<button v-if="onRetry" type="button" data-testid="error-alert-retry" @click="onRetry()">Retry</button>' +
    '<button v-if="onDismiss && dismissLabel" type="button" data-testid="error-alert-dismiss" @click="onDismiss()">{{ dismissLabel }}</button></div>',
}

function mountView() {
  return mount(SchemaListView, {
    global: {
      stubs: {
        LoadingSpinner: true,
        ErrorAlert: capturingErrorAlert,
      },
    },
  })
}

// PrimeVue Menu popup is teleported to document.body. Helper to open a row's
// action menu and return the teleported menu item matching the given label.
function findMenuItem(label: string): HTMLElement | null {
  const items = Array.from(document.body.querySelectorAll('[data-pc-section="item"]'))
  return (items.find((el) => el.textContent?.trim() === label) as HTMLElement) ?? null
}

describe('SchemaListView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // PageTabs resolves tier-locked state via the plan store (FAR-47).
    setActivePinia(createPinia())
    mockPush.mockClear()
    document.body.innerHTML = ''
    // Each test starts from a valid folder list; individual tests override it.
    getMock.mockResolvedValue([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ])
  })

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schemas')
    expect(wrapper.find('header.page-header + .page-tabs-wrapper').exists()).toBe(true)
    expect(wrapper.findAll('header.page-header + .page-tabs-wrapper .page-tab')).toHaveLength(3)
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

    // Open the menu for the active schema (row 1) -> deprecate is present
    await wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(findMenuItem('Deprecate')).not.toBeNull()

    // Open the menu for the deprecated schema (row 2) -> deprecate is absent
    await wrapper.find('[data-testid="schema-row-2"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(findMenuItem('Deprecate')).toBeNull()
  })

  it('opens, confirms, and cancels deprecation via the confirmation dialog', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    findMenuItem('Deprecate')!.click()
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
    await wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    findMenuItem('Deprecate')!.click()
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

  it('navigates to the schema editor on row keyboard activation (Enter and Space) for a11y', async () => {
    const wrapper = mountView()
    await flushPromises()

    const row = wrapper.find('[data-testid="schema-row-1"]')
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(mockPush).toHaveBeenCalledWith({ name: 'schema-editor', params: { id: '1' } })

    // Space is the keyboard-only equivalent of the row click.
    await row.trigger('keydown', { key: ' ' })
    await nextTick()
    expect(mockPush).toHaveBeenCalledTimes(2)
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

    await wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    findMenuItem('Move to Folder')!.click()
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

    await wrapper.find('[data-testid="schema-row-2"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    findMenuItem('Move to Folder')!.click()
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

  it('sets the drag payload and move effect when a schema drag starts, and tolerates a missing dataTransfer', async () => {
    const wrapper = mountView()
    await flushPromises()

    const dataTransfer = { setData: vi.fn(), effectAllowed: 'none' }
    await wrapper.find('[data-testid="schema-row-1"]').trigger('dragstart', { dataTransfer })
    expect(dataTransfer.setData).toHaveBeenCalledWith('text/plain', '1')
    expect(dataTransfer.effectAllowed).toBe('move')

    // The drag guard must not throw when the event carries no dataTransfer.
    await wrapper.find('[data-testid="schema-row-1"]').trigger('dragstart', { dataTransfer: null })
  })

  it('renders the selected-folder breadcrumb for a known folder and hides it for an unknown id', async () => {
    const wrapper = mountView()
    await flushPromises()

    const tree = wrapper.findComponent(FolderTree)
    tree.vm.$emit('select-folder', 'folder-1')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="schema-list-all-schemas"]').exists()).toBe(true)

    tree.vm.$emit('select-folder', 'folder-unknown')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="schema-list-all-schemas"]').exists()).toBe(false)
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

  it('surfaces a folder-load failure as the shared ErrorAlert with a working Retry (FAR-1655)', async () => {
    // First get() is the FolderTree's own fetch; the second is loadFolders.
    getMock.mockResolvedValueOnce([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ])
    getMock.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()

    const alert = wrapper.find('[data-testid="schema-list-folder-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load folders')
    expect(alert.text()).toContain('boom')
    // The mobile folder-error surface: a live region hidden at >=md (the
    // sidebar's FolderTree alert owns desktop) and NOT dismissible.
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.classes()).toContain('md:hidden')
    expect(alert.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)

    // Retry re-invokes loadFolders — the folder fetch succeeds this time.
    getMock.mockResolvedValue([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ])
    const getCallCountBefore = getMock.mock.calls.length
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()
    expect(getMock.mock.calls.length).toBeGreaterThan(getCallCountBefore)
    // loadFolders clears folderError on entry, so the alert unmounts.
    expect(wrapper.find('[data-testid="schema-list-folder-error"]').exists()).toBe(false)
  })

  it('does not offer a dismiss control on the folder-load failure alert (FAR-1655, revised)', async () => {
    // The folder-load failure is no longer dismissible: dismissing it left the
    // folder list empty with no retry until a full reload. The alert must
    // survive any attempt to dismiss it, exposing only Retry.
    // First get() call is the FolderTree's own fetch; the second is loadFolders.
    getMock.mockResolvedValueOnce([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ])
    getMock.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()

    const alert = wrapper.find('[data-testid="schema-list-folder-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)
    // Retry is the only recovery affordance, and it is present.
    expect(alert.find('[data-testid="error-alert-retry"]').exists()).toBe(true)

    // The alert stays put — the failure cannot be dismissed away.
    await nextTick()
    await flushPromises()
    expect(wrapper.find('[data-testid="schema-list-folder-error"]').exists()).toBe(true)
  })

  it('updates the folder-error message when a retry fails differently', async () => {
    // First get() is the FolderTree's own fetch; the second is loadFolders.
    getMock.mockResolvedValueOnce([
      { id: 'folder-1', organisation_id: 'org-1', name: 'Analytics', parent_id: null, sort_order: 0 },
    ])
    getMock.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()

    const alert = wrapper.find('[data-testid="schema-list-folder-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load folders: boom')

    // The retry fails DIFFERENTLY — the message must track the latest failure,
    // not stay frozen at the first one.
    getMock.mockRejectedValueOnce(new Error('second boom'))
    const getCallCountBefore = getMock.mock.calls.length
    await alert.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(getMock.mock.calls.length).toBeGreaterThan(getCallCountBefore)
    const alertAfter = wrapper.find('[data-testid="schema-list-folder-error"]')
    expect(alertAfter.exists()).toBe(true)
    expect(alertAfter.text()).toContain('second boom')
    expect(alertAfter.text()).not.toContain('Failed to load folders: boom')
  })

  it('splits the folder-error live region by breakpoint: main-panel alert is md:hidden, the sidebar alert is not', async () => {
    // jsdom applies no CSS, so class presence is the testable proxy for the
    // breakpoint contract. The main-panel alert is the complement of
    // FolderTree's `hidden md:flex`: exactly one is visible/announced, never
    // both and never neither. Both fetches fail here so both surfaces render.
    getMock.mockRejectedValue(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()

    const mainAlert = wrapper.find('[data-testid="schema-list-folder-error"]')
    expect(mainAlert.exists()).toBe(true)
    expect(mainAlert.attributes('role')).toBe('alert')
    expect(mainAlert.classes()).toContain('md:hidden')
    expect(mainAlert.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)

    const sidebarAlert = wrapper.find('[data-testid="folder-tree-error"]')
    expect(sidebarAlert.exists()).toBe(true)
    expect(sidebarAlert.attributes('role')).toBe('alert')
    // The complement: the sidebar owns >=md, so it must NOT be md:hidden. If
    // both gained md:hidden the pair would collapse to "both hidden".
    expect(sidebarAlert.classes()).not.toContain('md:hidden')
  })

  it('surfaces a schemas-load failure as the shared ErrorAlert and Retry re-fetches (FAR-1655)', async () => {
    const { api } = await import('../lib/api/client')
    const getMockApi = api.GET as ReturnType<typeof vi.fn>
    const originalGet = getMockApi.getMockImplementation()
    getMockApi.mockImplementation((url: string) => {
      if (url === '/api/v1/schemas') {
        return Promise.resolve({ data: undefined, error: { detail: 'schemas backend down' } })
      }
      return Promise.resolve({ data: { items: [], total: 0, page: 1, page_size: 100 }, error: undefined })
    })
    try {
      const wrapper = mountView()
      await flushPromises()

      const alert = wrapper.find('[data-testid="error-alert"]')
      expect(alert.exists()).toBe(true)
      expect(alert.text()).toContain('schemas backend down')

      getMockApi.mockImplementation((url: string) => {
        if (url === '/api/v1/schemas') {
          return Promise.resolve({
            data: { items: [{ id: '1', organisation_id: 'org-1', name: 'Recovered', description: null, abstract_name: null, folder_id: null, created_by: 'u', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', deprecated: false, deprecated_at: null }], total: 1, page: 1, page_size: 100 },
            error: undefined,
          })
        }
        return Promise.resolve({ data: { items: [], total: 0, page: 1, page_size: 100 }, error: undefined })
      })
      await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
      await flushPromises()

      expect(wrapper.text()).toContain('Recovered')
      expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
    } finally {
      getMockApi.mockImplementation(originalGet as (...args: unknown[]) => unknown)
    }
  })

  it('renders a deprecate-dialog failure as the shared ErrorAlert (FAR-1655)', async () => {
    const { api } = await import('../lib/api/client')
    const patchMockApi = api.PATCH as ReturnType<typeof vi.fn>
    const originalPatch = patchMockApi.getMockImplementation()
    patchMockApi.mockResolvedValue({ data: null, error: { detail: 'deprecate exploded' } })
    try {
      const wrapper = mountView()
      await flushPromises()

      await wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]').trigger('click')
      await flushPromises()
      await nextTick()
      findMenuItem('Deprecate')!.click()
      await flushPromises()
      await nextTick()

      const confirmBtn = document.body.querySelector('[data-testid="schema-deprecate-confirm"]') as HTMLElement | null
      expect(confirmBtn).not.toBeNull()
      confirmBtn!.click()
      await flushPromises()
      await nextTick()

      // The bespoke error <div> is gone; the shared ErrorAlert renders it.
      const alert = document.body.querySelector('[data-testid="error-alert"]') as HTMLElement | null
      expect(alert).not.toBeNull()
      expect(alert!.textContent).toContain('deprecate exploded')
      // A failed deprecate is retried via the dialog's Confirm button, so the
      // alert is explicitly non-retryable — no Retry control may render.
      expect(alert!.querySelector('[data-testid="error-alert-retry"]')).toBeNull()
    } finally {
      patchMockApi.mockImplementation(originalPatch as (...args: unknown[]) => unknown)
    }
  })

  it('surfaces a drag-to-folder move failure as the page-level shared ErrorAlert with working dismiss (FAR-1655)', async () => {
    patchMock.mockRejectedValueOnce(new Error('move exploded'))
    const wrapper = mountView()
    await flushPromises()

    // Drag-and-drop move goes through FolderTree's move-pipeline event.
    wrapper.findComponent(FolderTree).vm.$emit('move-pipeline', { pipelineId: '1', folderId: 'folder-1' })
    await flushPromises()

    // The bespoke move-error <div> is gone; the shared ErrorAlert keeps the
    // page-level testid / role="alert" semantics via attribute fallthrough.
    const alert = wrapper.find('[data-testid="schema-list-move-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('move exploded')

    // Explicitly non-retryable: a failed move can only be redone by re-dragging,
    // so no Retry control may render on the page-level alert.
    expect(alert.find('[data-testid="error-alert-retry"]').exists()).toBe(false)

    // Dismiss clears the error (same behaviour as the old X button).
    await wrapper.find('[data-testid="error-alert-dismiss"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="schema-list-move-error"]').exists()).toBe(false)
  })

  it('renders a move-dialog failure as the shared ErrorAlert without a retry button (FAR-1655)', async () => {
    patchMock.mockRejectedValueOnce(new Error('move exploded'))
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="schema-row-1"] [data-testid="schema-action-menu"]').trigger('click')
    await flushPromises()
    await nextTick()
    findMenuItem('Move to Folder')!.click()
    await flushPromises()
    await nextTick()

    const folderBtn = document.body.querySelector('[data-testid="schema-move-folder-folder-1"]') as HTMLElement | null
    expect(folderBtn).not.toBeNull()
    folderBtn!.click()
    await nextTick()

    const confirmBtn = document.body.querySelector('[data-testid="schema-move-confirm"]') as HTMLElement | null
    expect(confirmBtn).not.toBeNull()
    confirmBtn!.click()
    await flushPromises()
    await nextTick()

    // The dialog stays open on failure and shows the shared ErrorAlert.
    const dialog = document.body.querySelector('[data-testid="schema-move-dialog"]') as HTMLElement | null
    expect(dialog).not.toBeNull()
    expect(dialog!.textContent).toContain('move exploded')
    // No retry was offered on this box before the unification — Confirm
    // re-invokes handleMoveToFolder — so none may render now.
    expect(dialog!.querySelector('[data-testid="error-alert-retry"]')).toBeNull()
  })

  it('sorts the mobile folder select options alphabetically when API returns unsorted folders', async () => {
    getMock.mockResolvedValue([
      { id: 'f3', organisation_id: 'org-1', name: 'Zeta', parent_id: null, sort_order: 2 },
      { id: 'f1', organisation_id: 'org-1', name: 'alpha', parent_id: null, sort_order: 0 },
      { id: 'f2', organisation_id: 'org-1', name: 'Beta', parent_id: null, sort_order: 1 },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const select = wrapper.find('[data-testid="schema-list-mobile-folder-select"]')
    expect(select.exists()).toBe(true)
    // PrimeVue Select renders options in its props, not as native <option> elements
    const selectComponent = select.findComponent({ name: 'Select' })
    const options = selectComponent.props('options') as Array<{ value: string; label: string }>
    expect(options.map(o => o.label)).toEqual(['All Schemas', 'alpha', 'Beta', 'Zeta'])
  })

  it('opens the schema editor via keyboard (Enter / Space) for a11y (FAR-821)', async () => {
    mockPush.mockClear()
    const wrapper = mountView()
    await flushPromises()
    const row = wrapper.find('[data-testid="schema-row-1"]')
    expect(row.exists()).toBe(true)

    await row.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mockPush).toHaveBeenCalledWith({ name: 'schema-editor', params: { id: '1' } })

    mockPush.mockClear()
    await row.trigger('keydown', { key: ' ', code: 'Space' })
    await flushPromises()
    expect(mockPush).toHaveBeenCalledWith({ name: 'schema-editor', params: { id: '1' } })
  })

  it('keeps rendering when the schema-counts request fails, warning instead of crashing', async () => {
    const { api } = await import('../lib/api/client')
    const getMockApi = api.GET as ReturnType<typeof vi.fn>
    const originalGet = getMockApi.getMockImplementation()
    getMockApi.mockImplementation((url: string) => {
      if (url === '/api/v1/schemas/counts') return Promise.reject(new Error('counts down'))
      return Promise.resolve({ data: { items: [], total: 0, page: 1, page_size: 100 }, error: undefined })
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const wrapper = mountView()
      await flushPromises()

      expect(warn).toHaveBeenCalledWith('Failed to load schema counts')
      expect(wrapper.exists()).toBe(true)
    } finally {
      warn.mockRestore()
      getMockApi.mockImplementation(originalGet as (...args: unknown[]) => unknown)
    }
  })

  it('shows the folder load error and no folder list when the folder endpoint returns a non-array', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      // useApi.get is an unchecked `get<T>()` cast and returns `undefined as T` on
      // a 204; foldersList is dereferenced (foldersList.length / spread iteration)
      // on every render, so a non-array must be rejected, not assigned.
      getMock.mockResolvedValue(undefined)
      const wrapper = mountView()
      await flushPromises()
      await nextTick()
      expect(wrapper.exists()).toBe(true)
      expect(wrapper.findAll('[data-testid="schema-list-mobile-folder-select"]')).toHaveLength(0)
      expect(wrapper.text()).toContain('The server returned an invalid folder list.')
      expect(consoleError).toHaveBeenCalled()
    } finally {
      consoleError.mockRestore()
    }
  })
})
