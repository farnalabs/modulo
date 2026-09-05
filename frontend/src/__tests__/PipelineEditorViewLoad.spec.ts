import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

// NOTE: this spec mounts PipelineEditorView with the REAL useDataFetch —
// unlike PipelineEditorView.spec.ts, which mocks it. That matters because the
// view currently crashes on load (see the BUG characterisation below).
vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    }),
    post: vi.fn().mockResolvedValue({}),
  }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
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

describe('PipelineEditorView — page load (BUG characterisation)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('BUG: the editor page fails to load with a pageErrorRef TDZ error instead of the graph', async () => {
    // Production bug characterisation. PipelineEditorView.vue's useDataFetch
    // fetcher references `pageErrorRef` destructured from the SAME statement:
    //
    //   const { loading, error: pageErrorRef } = useDataFetch(async () => {
    //     pageErrorRef.value = null   // <- TDZ: binding not yet initialised
    //     await Promise.all([loadPipeline(), loadGraph(), ...])
    //   })
    //
    // vue-query invokes the fetcher synchronously while the useDataFetch call
    // is still in progress (QueryObserver#onSubscribe fires from the immediate
    // isRestoring watcher), so the fetcher throws
    // "Cannot access 'pageErrorRef' before initialization", vue-query captures
    // it as the query error, and the page renders the ERROR BOX with that
    // message instead of the editor. The graph, catalog, folders and lifecycle
    // maps never load. No e2e test navigates to /pipelines/:id/editor, so this
    // slipped through CI; the unit spec masked it by mocking useDataFetch.
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain("Cannot access 'pageErrorRef' before initialization")
    // the editor UI (toolbar + canvas) never renders
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
