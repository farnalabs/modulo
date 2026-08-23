import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 100 },
}

const { patchMock } = vi.hoisted(() => ({ patchMock: vi.fn() }))

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
    return Promise.resolve({ data: mockResponses.default, error: undefined })
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
    post: vi.fn(),
    patch: patchMock,
  }),
}))

import PipelineListView from '../views/PipelineListView.vue'

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
})
