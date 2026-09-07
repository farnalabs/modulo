import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 100 },
}

const { patchMock, postMock } = vi.hoisted(() => ({ patchMock: vi.fn(), postMock: vi.fn() }))

vi.mock('../lib/api/client', () => {
  const mockGet = vi.fn((url: string, options?: { params?: { query?: { page_size?: number } } }) => {
    if (url === '/api/v1/pipeline-folders') {
      return Promise.resolve({ data: mockResponses['/api/v1/pipeline-folders'] ?? [], error: undefined })
    }
    if (url === '/api/v1/pipelines') {
      const pageSize = options?.params?.query?.page_size ?? 100
      return Promise.resolve({
        data: mockResponses[`/api/v1/pipelines?page_size=${pageSize}`] ?? mockResponses.default,
        error: undefined,
      })
    }
    return Promise.resolve({ data: mockResponses[url] ?? mockResponses.default, error: undefined })
  })
  return {
    api: {
      GET: mockGet,
      PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn((url: string) => Promise.resolve(mockResponses[url] ?? [])),
    post: postMock,
    patch: patchMock,
  }),
}))

import PipelineListView from '../views/PipelineListView.vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines', name: 'pipeline-list', component: PipelineListView },
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: { template: '<div>editor</div>' } },
    { path: '/library', name: 'library', component: { template: '<div>library</div>' } },
  ],
})

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  localStorage.clear()
  mockResponses['/api/v1/pipelines?page_size=100'] = { items: [], total: 0, page: 1, page_size: 100 }
})

describe('PipelineListView', () => {
  it('renders without crashing', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    expect(wrapper.exists()).toBe(true)
  })

  it('sizes the page to the viewport minus the AppLayout chrome (sibling calc convention, never bare h-screen)', async () => {
    // PipelineEditorView/SchemaEditorView/CompositeEditorView all use
    // h-[calc(100vh-3.5rem)] because /pipelines renders below AppLayout's
    // breadcrumb/banner chrome — a bare h-screen root pushes the bottom of
    // the folder tree and table below the fold.
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    const classes = (wrapper.element as HTMLElement).className.split(/\s+/)
    expect(classes).toContain('h-[calc(100vh-3.5rem)]')
    expect(classes).not.toContain('h-screen')
  })

  it('renders the search bar with correct testid', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    expect(wrapper.find('[data-testid="filter-bar-search"]').exists()).toBe(true)
  })

  it('renders empty state when no pipelines exist', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No pipelines yet')
  })

  it('renders pipelines when data is returned', async () => {
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Test Pipeline', description: 'A test', visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Test Pipeline')
  })

  it('renders many pipelines on mount', async () => {
    const manyPipelines = Array.from({ length: 15 }, (_, i) => ({
      id: `p${i}`, organisation_id: 'org1', name: `Pipeline ${i}`, description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z',
    }))
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: manyPipelines,
      total: 15,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    expect(wrapper.exists()).toBe(true)
  })

  it('skips the PATCH when dropping a pipeline onto another in the same folder', async () => {
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Pipeline One', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f1' },
        { id: 'p2', organisation_id: 'org1', name: 'Pipeline Two', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f1' },
      ],
      total: 2,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    const folderTree = wrapper.findComponent({ name: 'FolderTree' })
    folderTree.vm.$emit('move-pipeline', { pipelineId: 'p1', folderId: 'f1' })
    await flushPromises()
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('shows an error banner when a drop-move fails', async () => {
    patchMock.mockRejectedValueOnce(new Error('Move failed'))
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Pipeline One', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f1' },
        { id: 'p2', organisation_id: 'org1', name: 'Pipeline Two', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: null },
      ],
      total: 2,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    const folderTree = wrapper.findComponent({ name: 'FolderTree' })
    folderTree.vm.$emit('move-pipeline', { pipelineId: 'p2', folderId: 'f1' })
    await flushPromises()
    expect(patchMock).toHaveBeenCalled()
    const banner = wrapper.find('[data-testid="pipeline-list-move-error"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Move failed')
  })

  it('persists folder expansion state across remounts', async () => {
    mockResponses['/api/v1/pipeline-folders'] = [
      { id: 'f1', organisation_id: 'org1', name: 'Folder One', parent_id: null, sort_order: 0 },
    ]
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Pipeline A', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f1' },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()

    // Folder is collapsed initially — children are not rendered
    const toggle = wrapper.find('[data-testid="pipeline-tree-folder-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-testid="pipeline-tree-row-p1"]').exists()).toBe(false)

    // Toggle expands the folder and persists the set to localStorage
    await toggle.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-tree-row-p1"]').exists()).toBe(true)
    expect(JSON.parse(localStorage.getItem('modulo.pipelines.expandedFolders') || '[]')).toContain('f1')

    // Unmount and remount: the expanded set survives via localStorage
    wrapper.unmount()
    const wrapper2 = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper2.find('[data-testid="pipeline-tree-row-p1"]').exists()).toBe(true)
  })

  it('offers a "Run as variant" action that deep-links to the ab-test creator with the pipeline id', async () => {
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Deep Link Pipe', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: {
          ErrorAlert: true,
          FolderTree: true,
          Menu: {
            props: ['model', 'popup'],
            template: '<div />',
            methods: { toggle: () => false },
          },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as {
      openActionMenu: (event: MouseEvent, p: unknown) => void
      actionMenuItems: Array<{ label: string; command: () => void }>
    }
    vm.openActionMenu({} as MouseEvent, { id: 'p1' })
    await nextTick()

    const runAsVariant = vm.actionMenuItems.find(i => i.label === 'Run as variant')
    expect(runAsVariant).toBeDefined()
    runAsVariant!.command()
    expect(router.push).toHaveBeenCalledWith({ path: '/variants/ab-test', query: { pipeline_id: 'p1' } })
  })

  it('auto-expands the selected folder and reflects expanded state in the toggle', async () => {
    mockResponses['/api/v1/pipeline-folders'] = [
      { id: 'f1', organisation_id: 'org1', name: 'Folder One', parent_id: null, sort_order: 0 },
    ]
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Pipeline A', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f1' },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()

    // Selecting the folder auto-expands its children even when not in expandedFolders
    wrapper.findComponent({ name: 'FolderTree' }).vm.$emit('select-folder', 'f1')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-tree-row-p1"]').exists()).toBe(true)
    const toggle = wrapper.find('[data-testid="pipeline-tree-folder-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('true')
  })

  it('renders an accessible modal dialog with Escape-to-close and a focus trap', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Rename Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    // Attach to the document: @vue/test-utils mounts detached by default, but
    // the focus-trap assertions below rely on document.activeElement, which only
    // updates for elements that are part of the live document.
    const wrapper = mount(PipelineListView, {
      attachTo: document.body,
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openRename: (p: typeof pipeline) => void }
    vm.openRename(pipeline)
    await nextTick()

    const dialog = wrapper.find('dialog')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.attributes('aria-label')).toBeTruthy()
    // The wrapping container must NOT be a button (the old ARIA violation).
    // The dialog is now a native <dialog> element (implicit role="dialog"),
    // so the old separate aria-hidden backdrop sibling no longer exists —
    // the backdrop is provided by the native ::backdrop pseudo-element.
    expect(dialog.element.parentElement?.getAttribute('role')).not.toBe('button')
    expect(dialog.element.tagName).toBe('DIALOG')

    // Tab focus is trapped: from the last control, Tab wraps to the first.
    const dialogEl = dialog.element as HTMLElement
    const focusables = dialogEl.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )
    expect(focusables.length).toBeGreaterThan(1)
    focusables[focusables.length - 1].focus()
    expect(document.activeElement).toBe(focusables[focusables.length - 1])
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }))
    await nextTick()
    expect(document.activeElement).toBe(focusables[0])

    // Escape closes the dialog.
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(wrapper.find('dialog').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders a Nodes column showing each pipeline node_count from the list response', async () => {
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Three Nodes', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', node_count: 3 },
        { id: 'p2', organisation_id: 'org1', name: 'No Count Yet', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      ],
      total: 2,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()

    const headers = wrapper.findAll('th')
    const nodesHeader = headers.find(h => h.text() === 'Nodes')
    expect(nodesHeader).toBeDefined()
    expect(nodesHeader!.attributes('scope')).toBe('col')
    // Backend always sends node_count (additive default 0) — a missing value
    // still renders the sensible 0, never "undefined".
    expect(wrapper.text()).toContain('Three Nodes')
    const rowCells = wrapper.findAll('td').map(td => td.text())
    expect(rowCells).toContain('3')
    expect(rowCells).toContain('0')
  })

  it('caps page_size at the backend maximum of 100 for runs and triggers fetches (no 422s)', async () => {
    // Backend caps page_size at le=100 on /api/v1/runs and /api/v1/triggers;
    // requesting 500 logs a 422 in the console on every page visit.
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Test Pipeline', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()

    const calls = (api.GET as unknown as ReturnType<typeof vi.fn>).mock.calls as Array<[string, { params?: { query?: { page_size?: number } } }]>
    const fetchCalls = calls.filter(([url]) => url === '/api/v1/runs' || url === '/api/v1/triggers')
    expect(fetchCalls.length).toBeGreaterThan(0)
    for (const [url, options] of fetchCalls) {
      expect(options?.params?.query?.page_size, `${url} page_size must be clamped to the backend max`).toBeLessThanOrEqual(100)
    }
  })

  it('opens the delete confirmation dialog and renders its i18n copy', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Delete Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openDelete: (p: typeof pipeline) => void }
    vm.openDelete(pipeline)
    await nextTick()

    const dialog = wrapper.find('[aria-label="Delete Pipeline"]')
    expect(dialog.exists()).toBe(true)
    // The UX-sweep i18n strings for the delete confirm dialog must render.
    expect(wrapper.text()).toContain('Are you sure? This permanently deletes the pipeline and all its runs.')
    const buttons = wrapper.findAll('button')
    expect(buttons.some(b => b.text() === 'Cancel')).toBe(true)
    expect(buttons.some(b => b.text() === 'Delete')).toBe(true)
  })

  it('opens the move-to-folder dialog and renders the folder choices with icons', async () => {
    mockResponses['/api/v1/pipeline-folders'] = [
      { id: 'f1', organisation_id: 'org1', name: 'Folder One', parent_id: null, sort_order: 0 },
    ]
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Move Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: null }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openMoveToFolder: (p: typeof pipeline) => void }
    vm.openMoveToFolder(pipeline)
    await flushPromises()
    await nextTick()

    const dialog = wrapper.find('[aria-label="Move to Folder"]')
    expect(dialog.exists()).toBe(true)
    // The folder choices (with their lucide icons) and the "No folder" option
    // introduced by the UX sweep must render.
    expect(wrapper.text()).toContain('Folder One')
    expect(wrapper.text()).toContain('No folder')
  })

  it('resolves the Last Run and Trigger columns from the runs/triggers APIs', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Dated Pipe', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    mockResponses['/api/v1/runs'] = { items: [{ id: 'r1', pipeline_id: 'p1', created_at: '2025-03-04T05:06:07Z' }], total: 1, page: 1, page_size: 100 }
    mockResponses['/api/v1/triggers'] = { items: [{ id: 't1', pipeline_id: 'p1', trigger_type: 'webhook' }], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()
    await flushPromises()
    await nextTick()
    // Last Run column must resolve from the newest run for the pipeline.
    expect(wrapper.text()).toContain('2025')
    // Trigger column must resolve from the newest trigger for the pipeline.
    expect(wrapper.text()).toContain('webhook')
  })

  it('filters the table by search term and shows the no-match state', async () => {
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Alpha Pipeline', description: 'first', visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
        { id: 'p2', organisation_id: 'org1', name: 'Beta Pipeline', description: 'second', visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      ],
      total: 2,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { search: string }
    vm.search = 'alpha'
    await nextTick()
    expect(wrapper.text()).toContain('Alpha Pipeline')
    expect(wrapper.text()).not.toContain('Beta Pipeline')

    // A search with no matches swaps the table for the empty-search state.
    vm.search = 'zzz-no-match'
    await nextTick()
    expect(wrapper.text()).toContain('No pipelines match your search')
  })

  it('navigates to the editor when a pipeline row is clicked', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Click Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="pipeline-tree-row-p1"]').trigger('click')
    expect(router.push).toHaveBeenCalledWith({ name: 'pipeline-editor', params: { id: 'p1' } })
  })

  it('drops a pipeline onto a folder row in the table to move it', async () => {
    mockResponses['/api/v1/pipeline-folders'] = [
      { id: 'f1', organisation_id: 'org1', name: 'Folder One', parent_id: null, sort_order: 0 },
      { id: 'f2', organisation_id: 'org1', name: 'Folder Two', parent_id: null, sort_order: 1 },
    ]
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Drag Pipe', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f1' },
        { id: 'p2', organisation_id: 'org1', name: 'Other Pipe', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: 'f2' },
      ],
      total: 2,
      page: 1,
      page_size: 100,
    }
    // Expand both folders so their rows render for the drag interaction.
    localStorage.setItem('modulo.pipelines.expandedFolders', JSON.stringify(['f1', 'f2']))
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const dataTransfer = {
      store: {} as Record<string, string>,
      setData(k: string, v: string) { this.store[k] = v },
      getData(k: string) { return this.store[k] },
      effectAllowed: '',
    }
    const row = wrapper.find('[data-testid="pipeline-tree-row-p1"]')
    const ds = new Event('dragstart') as unknown as DragEvent
    ;(ds as unknown as { dataTransfer: unknown }).dataTransfer = dataTransfer
    row.element.dispatchEvent(ds)

    const folderRows = wrapper.findAll('[data-testid="pipeline-tree-folder-row"]')
    const targetFolderRow = folderRows.find(r => r.text().includes('Folder Two'))
    expect(targetFolderRow).toBeDefined()
    const drop = new Event('drop') as unknown as DragEvent
    ;(drop as unknown as { dataTransfer: unknown }).dataTransfer = dataTransfer
    targetFolderRow!.element.dispatchEvent(drop)
    await flushPromises()
    expect(patchMock).toHaveBeenCalled()
  })

  it('renames a pipeline via the action menu and persists the new name', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Old Name', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openActionMenu: (e: MouseEvent, p: unknown) => void; actionMenuItems: Array<{ label: string; command: () => void }> }
    vm.openActionMenu({} as MouseEvent, pipeline)
    await nextTick()
    const rename = vm.actionMenuItems.find(i => i.label === 'Rename')
    expect(rename).toBeDefined()
    rename!.command()
    await nextTick()

    const input = wrapper.find('#pipelinelistview-field-1')
    expect(input.exists()).toBe(true)
    await input.setValue('New Name')
    // The save Button has no fixed aria-label; trigger its click by text.
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect((api.PATCH as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
  })

  it('archives and unarchives a pipeline via the action menu', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Archive Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openActionMenu: (e: MouseEvent, p: unknown) => void; actionMenuItems: Array<{ label: string; command: () => void }> }
    vm.openActionMenu({} as MouseEvent, pipeline)
    await nextTick()
    const archive = vm.actionMenuItems.find(i => i.label === 'Archive')
    expect(archive).toBeDefined()
    archive!.command()
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/pipelines/p1/archive')

    // Exercise the Unarchive branch by selecting an already-archived pipeline.
    vm.openActionMenu({} as MouseEvent, { ...pipeline, archived_at: '2025-02-02T00:00:00Z' })
    await nextTick()
    const unarchive = vm.actionMenuItems.find(i => i.label === 'Unarchive')
    expect(unarchive).toBeDefined()
    unarchive!.command()
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/pipelines/p1/unarchive')
  })

  it('deletes a pipeline via the action menu when the delete feature is enabled', async () => {
    const plan = usePlanStore()
    plan.features.pipeline_delete = true
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Delete Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' }
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openActionMenu: (e: MouseEvent, p: unknown) => void; actionMenuItems: Array<{ label: string; command: () => void }> }
    vm.openActionMenu({} as MouseEvent, pipeline)
    await nextTick()
    const del = vm.actionMenuItems.find(i => i.label === 'Delete')
    expect(del).toBeDefined()
    del!.command()
    await nextTick()

    const confirmBtn = wrapper.findAll('button').find(b => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flushPromises()
    expect((api.DELETE as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
  })

  it('saves a move-to-folder selection and refreshes the list', async () => {
    const pipeline = { id: 'p1', organisation_id: 'org1', name: 'Move Me', description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z', folder_id: null }
    mockResponses['/api/v1/pipeline-folders'] = [
      { id: 'f1', organisation_id: 'org1', name: 'Folder One', parent_id: null, sort_order: 0 },
    ]
    mockResponses['/api/v1/pipelines?page_size=100'] = { items: [pipeline], total: 1, page: 1, page_size: 100 }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: { plugins: [router], stubs: { ErrorAlert: true, FolderTree: true } },
    })
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as unknown as { openMoveToFolder: (p: typeof pipeline) => void }
    vm.openMoveToFolder(pipeline)
    await flushPromises()
    await nextTick()
    // Select the folder choice, then save.
    await wrapper.findAll('button').find(b => b.text() === 'Folder One')!.trigger('click')
    await nextTick()
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(patchMock).toHaveBeenCalled()
    // The move error banner is not shown on success.
    expect(wrapper.find('[data-testid="pipeline-list-move-error"]').exists()).toBe(false)
  })
})
