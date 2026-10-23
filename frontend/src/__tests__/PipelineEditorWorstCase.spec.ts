import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  return {
    useDataFetch: () => ({
      loading: ref(false),
      error: ref(null),
      data: ref(undefined),
      fetched: ref(true),
      load: async () => {},
    }),
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
  store.features = { pipeline_delete: true }
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
  useApiFns.get.mockReset()
  useApiFns.post.mockReset()
  useApiFns.get.mockResolvedValue({ items: [] })
  useApiFns.post.mockResolvedValue({})
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('PipelineEditorView worst-case node time (FAR-2072)', () => {
  it('renders the retry-panel and node ceilings from the stored retry policy', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: { max_retries: 2 }, node_timeout_seconds: 3300 }
    vm.retryPolicyOpen = true
    vm.retryPolicyMode = 'all'
    vm.retryPolicyMaxRetries = 2
    vm.selectedNodeData = {
      id: 'node-1',
      node_type: 'sandbox_agent',
      label: 'Node One',
      description: '',
      timeout_seconds: 3300,
      agent_commands: [],
    }
    await nextTick()

    const panelCeiling = wrapper.find('[data-testid="pipeline-editor-retry-policy-worst-case"]')
    expect(panelCeiling.exists()).toBe(true)
    expect(panelCeiling.text()).toContain('worst case: 3 attempts × 55m = 2h45m')

    const nodeCeiling = wrapper.find('[data-testid="pipeline-editor-node-worst-case"]')
    expect(nodeCeiling.exists()).toBe(true)
    expect(nodeCeiling.text()).toContain('worst case: 3 attempts × 55m = 2h45m')

    wrapper.unmount()
  })

  it('falls back to the pipeline default timeout when the node has none', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: { max_retries: 1 }, node_timeout_seconds: 600 }
    vm.retryPolicyOpen = false
    vm.selectedNodeData = {
      id: 'node-1',
      node_type: 'sandbox_agent',
      label: 'Node One',
      description: '',
      timeout_seconds: null,
      agent_commands: [],
    }
    await nextTick()

    expect(vm.storedPipelineRetryPolicy).toEqual({ max_retries: 1 })
    expect(vm.worstCaseTimeoutSeconds).toBe(600)
    expect(vm.selectedNodeWorstCase).toEqual({
      attempts: 2,
      timeoutSeconds: 600,
      totalSeconds: 1200,
    })

    wrapper.unmount()
  })

  it('has no ceiling when neither the node nor the pipeline sets a timeout', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: null, node_timeout_seconds: null }
    vm.retryPolicyOpen = false
    vm.selectedNodeData = {
      id: 'node-1',
      node_type: 'sandbox_agent',
      label: 'Node One',
      description: '',
      timeout_seconds: undefined,
      agent_commands: [],
    }
    await nextTick()

    expect(vm.storedPipelineRetryPolicy).toBeNull()
    expect(vm.worstCaseTimeoutSeconds).toBeNull()
    expect(vm.selectedNodeWorstCase).toBeNull()

    wrapper.unmount()
  })

  it('treats a malformed stored retry policy as absent', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    for (const retryPolicy of ['not-an-object', ['bad']]) {
      vm.pipeline = { retry_policy: retryPolicy, node_timeout_seconds: 3300 }
      expect(vm.storedPipelineRetryPolicy).toBeNull()
    }

    wrapper.unmount()
  })

  it('hides the panel ceiling when no timeout is configured', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: { max_retries: 2 }, node_timeout_seconds: null }
    vm.retryPolicyOpen = true
    // A zero budget exercises the falsy draft branch; no timeout leaves the
    // ceiling null so the v-if hides it.
    vm.retryPolicyMode = 'all'
    vm.retryPolicyMaxRetries = 0
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-worst-case"]').exists()).toBe(false)

    wrapper.unmount()
  })

  it('follows a specific-event draft policy while the panel is open', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: null, node_timeout_seconds: 3300 }
    vm.retryPolicyOpen = true
    vm.retryPolicyMode = 'specific'
    vm.retryPolicyEvents = ['timeout']
    vm.retryPolicyMaxRetries = 1
    await nextTick()

    const ceiling = wrapper.find('[data-testid="pipeline-editor-retry-policy-worst-case"]')
    expect(ceiling.exists()).toBe(true)
    expect(ceiling.text()).toContain('worst case: 2 attempts × 55m = 1h50m')

    wrapper.unmount()
  })

  it('hides the node ceiling when the timeout cannot be resolved to a number', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: null, node_timeout_seconds: null }
    vm.retryPolicyOpen = false
    vm.selectedNodeData = {
      id: 'node-1',
      node_type: 'sandbox_agent',
      label: 'Node One',
      description: '',
      // Truthy so the timeout block renders, but not resolvable to a duration.
      timeout_seconds: 'not-a-number',
      agent_commands: [],
    }
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-editor-node-worst-case"]').exists()).toBe(false)

    wrapper.unmount()
  })
})
