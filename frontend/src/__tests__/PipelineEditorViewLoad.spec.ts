import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

// NOTE: this spec mounts PipelineEditorView with the REAL useDataFetch —
// unlike PipelineEditorView.spec.ts, which mocks it — so the mount-time
// loader chain (loadPipeline/loadGraph/loadCatalog/loadFolders/
// loadLifecycleMaps) runs exactly as it does in the browser (FAR-629: the
// fetcher previously threw a pageErrorRef TDZ on every mount, which this
// suite now pins as fixed).

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

const apiGetMock = vi.hoisted(() => vi.fn())

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: apiGetMock,
    POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { usePlanStore } from '../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
  ],
})

function graphPayload() {
  return {
    nodes: [
      {
        id: 'node-1',
        node_type: 'agent',
        agent_id: 'agent-1',
        label: 'Agent Node',
        description: '',
        position: { x: 0, y: 0 },
      },
    ],
    edges: [],
  }
}

function seedApiSuccess() {
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: graphPayload(), error: undefined })
    }
    if (url.includes('/snapshots')) {
      return Promise.resolve({
        data: { items: [{ id: 's0', snapshot_version: 0 }, { id: 's2', snapshot_version: 2 }] },
        error: undefined,
      })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  })
  useApiFns.get.mockImplementation((url: string) => {
    if (url.includes('/pipeline-folders')) {
      return Promise.resolve([{ id: 'f-1', name: 'Prod', parent_id: null }])
    }
    if (url.endsWith('/lifecycle-maps')) {
      return Promise.resolve([{ id: 'lm-1' }])
    }
    if (url.includes('/lifecycle-maps/')) {
      return Promise.resolve({ id: 'lm-1', name: 'Checkout Flow', stages: [{ pipeline_id: 'test-pipeline-id' }] })
    }
    return Promise.resolve({ items: [] })
  })
}

function mountEditor() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  return mount(PipelineEditorView, {
    global: {
      plugins: [pinia, router],
      stubs: {
        VueFlow: { template: '<div><slot /></div>' },
        Background: true,
        Controls: true,
      },
    },
  })
}

// The global test setup mocks vue-router with a static route (params {}). The
// view captures `route.params.id` at setup, so seed it before every mount so
// pipeline-scoped calls and lifecycle-map matching carry the real id.
beforeEach(async () => {
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView — page load', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads the editor on mount: pipeline, graph, catalog, folders and lifecycle maps all land', async () => {
    seedApiSuccess()
    apiGetMock.mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: graphPayload(), error: undefined })
      }
      if (url.includes('/snapshots')) {
        return Promise.resolve({
          data: { items: [{ id: 's0', snapshot_version: 0 }, { id: 's2', snapshot_version: 2 }] },
          error: undefined,
        })
      }
      if (url.includes('/api/v1/agents')) {
        return Promise.resolve({ data: { items: [{ id: 'agent-1', name: 'Agent One' }] }, error: undefined })
      }
      if (url.includes('/pipelines/{pipeline_id}')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as any
    // the editor UI renders (toolbar + canvas), no error box
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Test Pipeline')
    expect(vm.pageError).toBeNull()
    // loadPipeline
    expect(vm.pipeline).toEqual({ id: 'test-pipeline-id', name: 'Test Pipeline' })
    // loadGraph
    expect(vm.rawNodes).toEqual(graphPayload().nodes)
    expect(vm.flowNodes[0].data.label).toBe('Agent Node')
    // loadCatalog (snapshot_version 0 is filtered out)
    expect(vm.agents).toEqual([{ id: 'agent-1', name: 'Agent One' }])
    expect(vm.snapshots).toEqual([{ id: 's2', snapshot_version: 2 }])
    // loadFolders
    expect(vm.folders).toEqual([{ id: 'f-1', name: 'Prod', parent_id: null }])
    // loadLifecycleMaps: only maps with a stage on THIS pipeline stay linked
    expect(vm.linkedLifecycleMaps).toEqual([
      { id: 'lm-1', name: 'Checkout Flow', stages: [{ pipeline_id: 'test-pipeline-id' }] },
    ])
    wrapper.unmount()
  })

  it('surfaces a graph load failure as a page error instead of the editor', async () => {
    seedApiSuccess()
    apiGetMock.mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: null, error: { detail: 'graph rejected' } })
      }
      if (url.includes('/pipelines/{pipeline_id}')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Failed to load graph: graph rejected')
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('surfaces a pipeline fetch rejection as a page error instead of the editor', async () => {
    seedApiSuccess()
    apiGetMock.mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: graphPayload(), error: undefined })
      }
      if (url.includes('/pipelines/{pipeline_id}')) {
        return Promise.reject(new Error('pipeline exploded'))
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Failed to load pipeline: pipeline exploded')
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the editor usable when the folder tree fails to load (fail-open loader)', async () => {
    seedApiSuccess()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/pipeline-folders')) return Promise.reject(new Error('folders down'))
      if (url.endsWith('/lifecycle-maps')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })

    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as any
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(true)
    expect(vm.pageError).toBeNull()
    expect(vm.folders).toEqual([])
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  it('keeps the editor usable when lifecycle maps fail to load (fail-open loader)', async () => {
    seedApiSuccess()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      if (url.includes('/lifecycle-maps')) return Promise.reject(new Error('maps down'))
      return Promise.resolve({ items: [] })
    })

    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as any
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(true)
    expect(vm.pageError).toBeNull()
    expect(vm.linkedLifecycleMaps).toEqual([])
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  it('keeps the editor usable when the catalog fails to load (fail-open loader)', async () => {
    seedApiSuccess()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    apiGetMock.mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: graphPayload(), error: undefined })
      }
      if (url.includes('/pipelines/{pipeline_id}')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
      }
      // agents/connectors/model-backends/schemas/snapshots/parameter-schemas
      return Promise.reject(new Error('catalog down'))
    })

    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    const vm = wrapper.vm as any
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(true)
    expect(vm.pageError).toBeNull()
    expect(vm.agents).toEqual([])
    expect(vm.snapshots).toEqual([])
    warnSpy.mockRestore()
    wrapper.unmount()
  })
})
