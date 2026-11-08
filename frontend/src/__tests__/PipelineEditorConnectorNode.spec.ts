import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'

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
    if (url.includes('/snapshots/')) {
      return Promise.resolve({
        data: { id: 'snap-1', graph_json: { nodes: [], edges: [] } },
        error: undefined,
      })
    }
    if (url.includes('/api/v1/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    if (url.includes('/api/v1/connectors')) {
      return Promise.resolve({
        data: { items: [{ id: 'conn-1', name: 'GitHub Org', connector_type_id: 'github' }] },
        error: undefined,
      })
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

async function mountReady() {
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  const wrapper = mountEditor()
  await flushPromises()
  const vm = wrapper.vm as any
  vm.connectors = [{ id: 'conn-1', name: 'GitHub Org', connector_type_id: 'github' }]
  return { wrapper, vm }
}

beforeEach(async () => {
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => {
    params: Record<string, string>
    query: Record<string, string>
  })()
  route.params = { id: 'test-pipeline-id' }
  route.query = {}
  vi.clearAllMocks()
  useApiFns.get.mockReset()
  useApiFns.post.mockReset()
  useApiFns.get.mockImplementation((url: string) => {
    if (url.includes('/lifecycle-maps')) return Promise.resolve([])
    if (url.includes('/pipeline-folders')) return Promise.resolve([])
    if (url.includes('/api/v1/connectors')) {
      return Promise.resolve({ items: [{ id: 'conn-1', name: 'GitHub Org', connector_type_id: 'github' }] })
    }
    return Promise.resolve({ items: [] })
  })
  useApiFns.post.mockResolvedValue({})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('PipelineEditorView — FAR-1395 connector query authoring', () => {
  it('offers the connector node type in the palette', async () => {
    const { vm } = await mountReady()

    const values = (vm.nodeTypeOptions as Array<{ value: string }>).map((o) => o.value)
    expect(values).toContain('connector')
  })

  it('addNode("connector") creates a raw connector node with a binding skeleton', async () => {
    const { vm } = await mountReady()

    vm.addNode('connector')

    const node = vm.rawNodes[vm.rawNodes.length - 1]
    expect(node.node_type).toBe('connector')
    expect(node.connector_binding).toEqual({ type: '', instance_id: '', operation: 'query' })
  })

  it('choosing a connector fills the instance id and declared connector type', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeInstanceChange({ target: { value: 'conn-1' } } as unknown as Event)

    expect(vm.selectedNodeData.connector_binding.instance_id).toBe('conn-1')
    expect(vm.selectedNodeData.connector_binding.type).toBe('github')
  })

  it('the operation select only ever authors query or write', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeOperationChange({ target: { value: 'write' } } as unknown as Event)
    expect(vm.selectedNodeData.connector_binding.operation).toBe('write')

    // An out-of-vocabulary value can never be authored from the UI.
    vm.onConnectorNodeOperationChange({ target: { value: 'frobnicate' } } as unknown as Event)
    expect(vm.selectedNodeData.connector_binding.operation).toBe('query')
  })

  it('a resource is stored, and a blank resource clears it', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeResourceInput({ target: { value: 'pr_files' } } as unknown as Event)
    expect(vm.selectedNodeData.connector_binding.resource).toBe('pr_files')

    vm.onConnectorNodeResourceInput({ target: { value: '   ' } } as unknown as Event)
    expect('resource' in vm.selectedNodeData.connector_binding).toBe(false)
  })

  it('valid filters JSON is committed to the binding', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeJsonInput('filters', {
      target: { value: '{"owner_repo": "x/y", "pull_number": 7}' },
    } as unknown as Event)

    expect(vm.selectedNodeData.connector_binding.filters).toEqual({ owner_repo: 'x/y', pull_number: 7 })
    expect(vm.connectorNodeJsonError).toBeNull()
  })

  it('invalid JSON surfaces an error and keeps the last valid value', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeJsonInput('filters', { target: { value: '{"ok": true}' } } as unknown as Event)
    vm.onConnectorNodeJsonInput('filters', { target: { value: '{not json' } } as unknown as Event)

    expect(vm.connectorNodeJsonError).toContain('filters')
    expect(vm.selectedNodeData.connector_binding.filters).toEqual({ ok: true })
  })

  it('non-object JSON (an array) is refused too', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeJsonInput('data', { target: { value: '[1, 2]' } } as unknown as Event)

    expect(vm.connectorNodeJsonError).toContain('data')
    expect('data' in vm.selectedNodeData.connector_binding).toBe(false)
  })

  it('buildNodePayload carries the authored binding into the graph save', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    const node = vm.rawNodes[0]
    node.connector_binding = {
      type: 'github',
      instance_id: 'conn-1',
      operation: 'query',
      resource: 'pr_files',
      filters: { pull_number: 7 },
      data: { note: 'x' },
    }

    const payload = vm.buildNodePayload(node)

    expect(payload.node_type).toBe('connector')
    expect(payload.connector_binding).toEqual({
      type: 'github',
      instance_id: 'conn-1',
      operation: 'query',
      resource: 'pr_files',
      filters: { pull_number: 7 },
      data: { note: 'x' },
    })
  })

  it('renders the connector binding controls for a selected connector node', async () => {
    const { wrapper, vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]
    await flushPromises()

    expect(wrapper.find('[data-testid="pipeline-editor-connector-node-instance"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-connector-node-operation"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-connector-node-resource"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-connector-node-filters"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-connector-node-data"]').exists()).toBe(true)
  })

  it('renders the controls for a connector node that has no binding yet', async () => {
    const { wrapper, vm } = await mountReady()
    vm.selectedNodeData = { id: 'node-unbound', node_type: 'connector' }
    await flushPromises()

    const operation = wrapper.find('[data-testid="pipeline-editor-connector-node-operation"]')
    expect(operation.exists()).toBe(true)
    // The unset verb falls back to the engine's query default in the UI.
    expect((operation.element as HTMLSelectElement).value).toBe('query')
  })

  it('ensureConnectorNodeBinding repairs a missing or non-object binding', async () => {
    const { vm } = await mountReady()

    vm.selectedNodeData = { id: 'node-missing', node_type: 'connector' }
    expect(vm.ensureConnectorNodeBinding()).toEqual({ type: '', instance_id: '', operation: 'query' })
    expect(vm.selectedNodeData.connector_binding).toEqual({ type: '', instance_id: '', operation: 'query' })

    vm.selectedNodeData = { id: 'node-bad', node_type: 'connector', connector_binding: 'nonsense' }
    expect(vm.ensureConnectorNodeBinding()).toEqual({ type: '', instance_id: '', operation: 'query' })
  })

  it('the authoring handlers are no-ops when no node is selected', async () => {
    const { vm } = await mountReady()
    vm.selectedNodeData = null

    vm.onConnectorNodeInstanceChange({ target: { value: 'conn-1' } } as unknown as Event)
    vm.onConnectorNodeOperationChange({ target: { value: 'write' } } as unknown as Event)
    vm.onConnectorNodeResourceInput({ target: { value: 'pr_files' } } as unknown as Event)
    vm.onConnectorNodeJsonInput('filters', { target: { value: '{"a": 1}' } } as unknown as Event)

    expect(vm.selectedNodeData).toBeNull()
  })

  it('a blank filters/data draft commits an empty object', async () => {
    const { vm } = await mountReady()
    vm.addNode('connector')
    vm.selectedNodeData = vm.rawNodes[0]

    vm.onConnectorNodeJsonInput('data', { target: { value: '   ' } } as unknown as Event)

    expect(vm.selectedNodeData.connector_binding.data).toEqual({})
    expect(vm.connectorNodeJsonError).toBeNull()
  })

  it('the authoring handlers are inert while viewing a historic snapshot', async () => {
    const { useRoute } = await import('vue-router')
    const route = (useRoute as unknown as () => { query: Record<string, string> })()
    route.query = { snapshot: 'snap-1' }

    const { vm } = await mountReady()
    expect(vm.isViewing).toBe(true)

    vm.selectedNodeData = {
      id: 'node-viewing',
      node_type: 'connector',
      connector_binding: { type: 'github', instance_id: 'conn-0', operation: 'query' },
    }
    const before = JSON.stringify(vm.selectedNodeData.connector_binding)

    vm.onConnectorNodeInstanceChange({ target: { value: 'conn-1' } } as unknown as Event)
    vm.onConnectorNodeOperationChange({ target: { value: 'write' } } as unknown as Event)
    vm.onConnectorNodeResourceInput({ target: { value: 'pr_files' } } as unknown as Event)
    vm.onConnectorNodeJsonInput('filters', { target: { value: '{"a": 1}' } } as unknown as Event)

    expect(JSON.stringify(vm.selectedNodeData.connector_binding)).toBe(before)
  })
})
