import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'

// FAR-1141 (frontend slice): run-provenance badge + `dispatch` node-type
// display recognition. Covers the pure helpers, the locale keys, and the
// pipeline editor's node-type maps.

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  const loadingRef = ref(false)
  return {
    useDataFetch: () => ({
      loading: loadingRef,
      error: ref(null),
      data: ref(undefined),
      fetched: ref(true),
      load: async () => {},
    }),
    __loadingRef: loadingRef,
  }
})

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

vi.mock('../lib/api/client', () => {
  const get = (url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    }
    if (url.includes('/api/v1/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    if (url.includes('/parameter-schemas') && url.includes('/sets')) {
      return Promise.resolve({ data: [], error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  }
  return {
    api: {
      GET: vi.fn(get),
      POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { usePlanStore } from '../stores/planStore'
import { isDispatchedRun } from '../utils/runUtils'
import { layoutNodes } from '../utils/graph-layout'
import enUS from '../locales/en-US.js'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
    { path: '/runs/:id', name: 'run-detail', component: { template: '<div />' } },
    { path: '/library', name: 'library', component: { template: '<div />' } },
  ],
})

function mountEditor() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = { pipeline_delete: true, pipeline_diff_rollback: true }
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

beforeEach(async () => {
  vi.clearAllMocks()
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('isDispatchedRun (run provenance predicate)', () => {
  it('treats only the "dispatched" origin as dispatched', () => {
    expect(isDispatchedRun('dispatched')).toBe(true)
  })

  it('never treats executed/legacy/absent origins as dispatched', () => {
    expect(isDispatchedRun(null)).toBe(false)
    expect(isDispatchedRun(undefined)).toBe(false)
    expect(isDispatchedRun('executed')).toBe(false)
    expect(isDispatchedRun('')).toBe(false)
  })
})

describe('locale keys for dispatch provenance and node type', () => {
  it('carries every key the dispatch surfaces render', () => {
    const messages = enUS as Record<string, Record<string, any>>
    expect(messages.common.execution_origin.dispatched).toBe('Dispatched')
    expect(typeof messages.common.execution_origin.dispatched_hint).toBe('string')
    expect(messages.views.PipelineEditorView.node_dispatch_label).toBe('Dispatch')
    expect(messages.views.PipelineEditorView.node_dispatch_badge).toBe('DISPATCH')
    expect(messages.views.CompositeEditorView.node_dispatch_badge).toBe('DISPATCH')
  })
})

describe('graph-layout dispatch pass-through', () => {
  it('keeps a dispatch node on its own canvas type instead of the agent fallback', () => {
    const [node] = layoutNodes([{ id: 'd', node_type: 'dispatch', label: 'Fire job' }], [])
    expect(node.type).toBe('dispatch')
  })
})

describe('PipelineEditorView dispatch node-type recognition', () => {
  it('labels, colours and converts a dispatch node distinctly from an agent node', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // Label: dispatch must not fall through to the generic agent label.
    expect(vm.nodeTypeLabel('dispatch')).toBe('Dispatch')
    expect(vm.nodeTypeLabel('dispatch')).not.toBe(vm.nodeTypeLabel('agent'))

    // Badge: its own (non-alarming) colour, not the default primary/agent one.
    expect(vm.nodeTypeBadgeClass('dispatch')).toContain('cyan')

    // Canvas type: convertBackendNode must not collapse dispatch into agent.
    const converted = vm.convertBackendNode({ id: 'd1', node_type: 'dispatch', label: 'Fire job', position: { x: 0, y: 0 } })
    expect(converted.type).toBe('dispatch')
    expect(converted.data.node_type).toBe('dispatch')

    // Registered alongside the other canvas node types.
    expect(vm.nodeTypes.dispatch).toBe('dispatch')

    wrapper.unmount()
  })
})
