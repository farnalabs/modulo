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
      return Promise.resolve({
        data: { nodes: [], edges: [] },
        error: undefined,
      })
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

import { api } from '../lib/api/client'
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
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('PipelineEditorView — coverage: script logic branches', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useApiFns.get.mockReset()
    useApiFns.post.mockReset()
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    useApiFns.post.mockResolvedValue({})
  })

  // -- nodeTypeBadgeClass / nodeTypeLabel: all branches --
  it('nodeTypeBadgeClass returns correct class for each node type', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    expect(vm.nodeTypeBadgeClass('manual')).toContain('warning')
    expect(vm.nodeTypeBadgeClass('router')).toContain('indigo')
    expect(vm.nodeTypeBadgeClass('hitl')).toContain('rose')
    expect(vm.nodeTypeBadgeClass('agent')).toContain('primary')
    expect(vm.nodeTypeBadgeClass('sandbox_agent')).toContain('primary')

    expect(vm.nodeTypeLabel('manual')).toBeTruthy()
    expect(vm.nodeTypeLabel('sandbox_agent')).toBeTruthy()
    expect(vm.nodeTypeLabel('router')).toBeTruthy()
    expect(vm.nodeTypeLabel('hitl')).toBeTruthy()
    expect(vm.nodeTypeLabel('agent')).toBeTruthy()

    wrapper.unmount()
  })

  // -- connectorName: all branches --
  it('connectorName handles null, found, and fallback branches', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.connectors = [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }]

    // null binding
    expect(vm.connectorName(null)).toBe('-')
    // found connector
    expect(vm.connectorName({ type: 'slack', instance_id: 'conn-1' })).toBe('Slack Dev (slack)')
    // fallback: connector not found, but instance_id present
    expect(vm.connectorName({ type: 'github', instance_id: 'conn-99' })).toContain('conn-99')
    // fallback: no instance_id at all
    expect(vm.connectorName({ type: 'github' })).toBe('github')

    wrapper.unmount()
  })

  // -- addCapabilityEntry: empty input, duplicate, and both fields --
  it('addCapabilityEntry skips empty input and deduplicates', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // empty input: should not add
    vm.capabilityToolInput = '   '
    vm.addCapabilityEntry('allowed_tools')
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual([])

    // whitespace-only input
    vm.capabilityToolInput = ''
    vm.addCapabilityEntry('allowed_tools')
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual([])

    // valid input
    vm.capabilityToolInput = 'tool-a'
    vm.addCapabilityEntry('allowed_tools')
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual(['tool-a'])
    expect(vm.capabilityToolInput).toBe('')

    // duplicate: should not add again
    vm.capabilityToolInput = 'tool-a'
    vm.addCapabilityEntry('allowed_tools')
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual(['tool-a'])

    // context_scope field
    vm.capabilityContextInput = 'ctx-1'
    vm.addCapabilityEntry('context_scope')
    expect(vm.nodeCapabilityScope.context_scope).toEqual(['ctx-1'])
    expect(vm.capabilityContextInput).toBe('')

    wrapper.unmount()
  })

  // -- removeCapabilityEntry --
  it('removeCapabilityEntry removes the value and no-ops for non-existent value', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vm.nodeCapabilityScope.allowed_tools = ['tool-a', 'tool-b']
    vm.removeCapabilityEntry('allowed_tools', 'tool-a')
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual(['tool-b'])

    // removing non-existent: no error
    vm.removeCapabilityEntry('allowed_tools', 'tool-x')
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual(['tool-b'])

    // context_scope
    vm.nodeCapabilityScope.context_scope = ['ctx-1']
    vm.removeCapabilityEntry('context_scope', 'ctx-1')
    expect(vm.nodeCapabilityScope.context_scope).toEqual([])

    wrapper.unmount()
  })

  // -- syncCapabilityScopeToNode: null when all empty --
  it('syncCapabilityScopeToNode sets null when all scope arrays are empty', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 }, capability_scope: { allowed_tools: ['tool-a'] } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // clear all scope
    vm.nodeCapabilityScope.allowed_connectors = []
    vm.nodeCapabilityScope.allowed_tools = []
    vm.nodeCapabilityScope.context_scope = []
    await nextTick()

    expect(vm.selectedNodeData.capability_scope).toBeNull()
    wrapper.unmount()
  })

  // -- outOfScopeConnectors computed --
  it('outOfScopeConnectors detects connectors not in the available set', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }]
    vm.connectors = [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // conn-1 is available (slack type), so it is in-scope
    vm.nodeCapabilityScope.allowed_connectors = ['conn-1']
    expect(vm.outOfScopeConnectors).toEqual([])

    // conn-99 is NOT in the available connectors, so it's out-of-scope
    vm.nodeCapabilityScope.allowed_connectors = ['conn-99']
    expect(vm.outOfScopeConnectors).toEqual(['conn-99'])

    wrapper.unmount()
  })

  // -- doesNodeHaveCapabilityScope --
  it('doesNodeHaveCapabilityScope returns false for null/undefined scope', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    expect(vm.doesNodeHaveCapabilityScope({})).toBe(false)
    expect(vm.doesNodeHaveCapabilityScope({ capability_scope: null })).toBe(false)
    expect(vm.doesNodeHaveCapabilityScope({ capability_scope: {} })).toBe(false)
    expect(vm.doesNodeHaveCapabilityScope({ capability_scope: { allowed_connectors: [] } })).toBe(false)
    expect(vm.doesNodeHaveCapabilityScope({ capability_scope: { allowed_connectors: ['a'] } })).toBe(true)

    wrapper.unmount()
  })

  // -- onNodeClick with null/missing node --
  it('onNodeClick handles null node gracefully', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.onNodeClick({ node: null })
    expect(vm.selectedNodeData).toBeNull()

    vm.onNodeClick({ node: { id: 'nonexistent-id' } })
    expect(vm.selectedNodeData).toBeNull()

    wrapper.unmount()
  })

  // -- onEdgeClick with null edge --
  it('onEdgeClick handles null edge gracefully', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.onEdgeClick({ edge: null })
    expect(vm.selectedEdgeData).toBeNull()

    vm.onEdgeClick({ edge: { id: 'nonexistent-edge' } })
    expect(vm.selectedEdgeData).toBeNull()

    wrapper.unmount()
  })

  // -- populateEdgeForm: all condition branches --
  it('populateEdgeForm handles jmespath, eval, and none condition types', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // jmespath condition
    vm.populateEdgeForm({
      edge_type: 'loop', max_iterations: 5, routing_label: '',
      hitl_gate_config: { label: 'Gate', description: 'Approve the deploy only after a human reviews the plan.', claim_expiry_minutes: 30, human_only: false, condition: 'status == "ok"' },
    })
    expect(vm.edgeForm.hitl_enabled).toBe(true)
    expect(vm.edgeForm.condition_type).toBe('jmespath')
    expect(vm.edgeForm.condition).toBe('status == "ok"')
    expect(vm.edgeForm.human_only).toBe(false)
    expect(vm.edgeForm.edge_type).toBe('loop')
    expect(vm.edgeForm.max_iterations).toBe(5)

    // eval condition
    vm.populateEdgeForm({
      edge_type: 'llm', routing_label: 'go',
      hitl_gate_config: { label: 'Gate', description: 'Approve the deploy only after a human reviews the plan.', claim_expiry_minutes: 15, eval_condition: { eval_name: 'quality', threshold: 0.9, operator: 'gte' } },
    })
    expect(vm.edgeForm.condition_type).toBe('eval')
    expect(vm.edgeForm.eval_name).toBe('quality')
    expect(vm.edgeForm.eval_threshold).toBe(0.9)
    expect(vm.edgeForm.eval_operator).toBe('gte')
    expect(vm.edgeForm.condition).toBe('')
    expect(vm.edgeForm.edge_type).toBe('llm')
    expect(vm.edgeForm.routing_label).toBe('go')

    // none condition (no condition or eval_condition in the config)
    vm.populateEdgeForm({
      edge_type: 'reject',
      hitl_gate_config: { label: 'Gate', description: 'Approve the deploy only after a human reviews the plan.' },
    })
    expect(vm.edgeForm.condition_type).toBe('none')
    expect(vm.edgeForm.condition).toBe('')
    expect(vm.edgeForm.eval_name).toBe('')
    expect(vm.edgeForm.edge_type).toBe('reject')

    // no hitl_gate_config (gate-less edge)
    vm.populateEdgeForm({ edge_type: 'loop', max_iterations: 3, routing_label: 'retry' })
    expect(vm.edgeForm.hitl_enabled).toBe(false)
    expect(vm.edgeForm.edge_type).toBe('loop')
    expect(vm.edgeForm.max_iterations).toBe(3)
    expect(vm.edgeForm.routing_label).toBe('retry')

    wrapper.unmount()
  })

  // -- buildHitlGateConfig: all condition branches --
  it('buildHitlGateConfig returns null when HITL is disabled', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.edgeForm.hitl_enabled = false
    expect(vm.buildHitlGateConfig()).toBeNull()

    // jmespath condition
    vm.edgeForm.hitl_enabled = true
    vm.edgeForm.condition_type = 'jmespath'
    vm.edgeForm.condition = 'status == "ok"'
    vm.edgeForm.label = 'My Gate'
    vm.edgeForm.description = 'A gate description for testing'
    vm.edgeForm.claim_expiry_minutes = 15
    vm.edgeForm.human_only = true
    vm.selectedEdgeData = { hitl_gate_config: { reject_target: 'node-x', required_team_id: 'team-1' } }
    const config = vm.buildHitlGateConfig()
    expect(config.condition).toBe('status == "ok"')
    expect(config.eval_condition).toBeUndefined()
    expect(config.reject_target).toBe('node-x')
    expect(config.required_team_id).toBe('team-1')

    // eval condition
    vm.edgeForm.condition_type = 'eval'
    vm.edgeForm.eval_name = 'quality'
    vm.edgeForm.eval_threshold = 0.9
    vm.edgeForm.eval_operator = 'gte'
    vm.edgeForm.condition = ''
    const evalConfig = vm.buildHitlGateConfig()
    expect(evalConfig.eval_condition).toEqual({ eval_name: 'quality', threshold: 0.9, operator: 'gte' })
    expect(evalConfig.condition).toBeUndefined()

    // none condition
    vm.edgeForm.condition_type = 'none'
    const noneConfig = vm.buildHitlGateConfig()
    expect(noneConfig.condition).toBeUndefined()
    expect(noneConfig.eval_condition).toBeUndefined()

    wrapper.unmount()
  })

  // -- saveGraph: builds correct edge payloads --
  it('saveGraph builds edge payloads with max_iterations and routing_label conditionally', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'n1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]
    vm.rawEdges = [
      { id: 'e1', source_node_id: 'n1', target_node_id: 'n1', edge_type: 'loop', max_iterations: 3, condition_expression: 'x > 1', hitl_gate_config: null, source_port: 'out', target_port: 'in' },
      { id: 'e2', source_node_id: 'n1', target_node_id: 'n1', edge_type: 'llm', routing_label: 'retry', condition_expression: null, hitl_gate_config: null },
    ]

    await vm.saveGraph()
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedEdges = (patch[1] as any).body.edges
    const loopEdge = savedEdges.find((e: any) => e.id === 'e1')
    expect(loopEdge.max_iterations).toBe(3)
    expect(loopEdge.condition_expression).toBe('x > 1')
    expect(loopEdge.routing_label).toBeUndefined()
    const llmEdge = savedEdges.find((e: any) => e.id === 'e2')
    expect(llmEdge.routing_label).toBe('retry')
    expect(llmEdge.max_iterations).toBeUndefined()

    wrapper.unmount()
  })

  // -- buildNodePayload strips VIEW_ONLY keys --
  it('buildNodePayload strips UI-only keys and preserves all model keys', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const payload = vm.buildNodePayload({
      id: 'n1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 },
      type: 'agent', data: { label: 'A' }, selected: true, dragging: false,
      dimensions: { width: 100, height: 50 }, hasCapabilityScope: true,
      model_backend_id: 'mb-1', agent_command: 'legacy-scalar',
    })
    expect(payload).not.toHaveProperty('type')
    expect(payload).not.toHaveProperty('data')
    expect(payload).not.toHaveProperty('selected')
    expect(payload).not.toHaveProperty('dragging')
    expect(payload).not.toHaveProperty('dimensions')
    expect(payload).not.toHaveProperty('hasCapabilityScope')
    expect(payload).not.toHaveProperty('model_backend_id')
    expect(payload).not.toHaveProperty('agent_command')
    expect(payload.node_type).toBe('agent')
    expect(payload.label).toBe('A')

    wrapper.unmount()
  })

  // -- loadGraph: catch branch for network error --
  it('loadGraph catches network errors and sets pageError', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.reject(new Error('network_timeout'))
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    await vm.loadGraph()
    await flushPromises()
    expect(vm.pageError).toContain('network_timeout')
    wrapper.unmount()
  })

  // -- loadCatalog: catch branch --
  it('loadCatalog catches errors and keeps empty arrays', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation(() => {
      return Promise.reject(new Error('catalog_explosion'))
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    await vm.loadCatalog()
    await flushPromises()
    expect(vm.agents).toEqual([])
    expect(vm.connectors).toEqual([])
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- loadFolders: catch branch --
  it('loadFolders catches errors and keeps empty array', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/pipeline-folders')) return Promise.reject(new Error('folders_down'))
      return Promise.resolve([])
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    await vm.loadFolders()
    await flushPromises()
    expect(vm.folders).toEqual([])
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- loadLifecycleMaps: catch branch and items shape --
  it('loadLifecycleMaps catches errors and handles items response shape', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps') && !url.includes('/lm-')) {
        return Promise.resolve({ items: [{ id: 'lm-1' }, { id: 'lm-2' }] })
      }
      if (url.includes('/lifecycle-maps/lm-1')) {
        return Promise.resolve({ id: 'lm-1', name: 'Map One', stages: [{ pipeline_id: 'test-pipeline-id' }] })
      }
      if (url.includes('/lifecycle-maps/lm-2')) {
        return Promise.reject(new Error('map_down'))
      }
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    await vm.loadLifecycleMaps()
    await flushPromises()
    // lm-1 matched, lm-2 failed (caught)
    expect(vm.linkedLifecycleMaps.length).toBe(1)
    expect(vm.linkedLifecycleMaps[0].id).toBe('lm-1')
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- loadLifecycleMaps: full list not an array --
  it('loadLifecycleMaps handles non-array response gracefully', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps') && !url.includes('/lm-')) {
        return Promise.resolve(null)
      }
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    await vm.loadLifecycleMaps()
    await flushPromises()
    expect(vm.linkedLifecycleMaps).toEqual([])
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- loadGraph: graph data with nodes --
  it('loadGraph processes nodes and edges into flow format', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({
          data: {
            nodes: [
              { id: 'n1', node_type: 'agent', label: 'Agent', description: '', position: { x: 10, y: 20 } },
            ],
            edges: [
              { id: 'e1', source_node_id: 'n1', target_node_id: 'n1', edge_type: 'normal' },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    await vm.loadGraph()
    await flushPromises()
    expect(vm.rawNodes.length).toBe(1)
    expect(vm.flowNodes.length).toBe(1)
    expect(vm.rawEdges.length).toBe(1)
    expect(vm.flowEdges.length).toBe(1)
    wrapper.unmount()
  })

  // -- triggerRun: saveGraph error path --
  it('triggerRun returns early when pipeline is null', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = null

    await vm.triggerRun()
    // should not have called POST
    expect(vi.mocked(api.POST).mock.calls.filter(c => String(c[0]).includes('/runs')).length).toBe(0)
    wrapper.unmount()
  })

  // -- triggerRun: navigating to run detail on success --
  it('triggerRun navigates to run-detail on success', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'Agent', description: '', position: { x: 0, y: 0 } }]
    await nextTick()

    ;(api.POST as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: { id: 'run-123' }, error: undefined })
    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="pipeline-editor-run-prompt"]').setValue('test')
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    const pushSpy = vi.spyOn(router, 'push')
    // Re-run to get the navigation
    ;(api.POST as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: { id: 'run-456' }, error: undefined })
    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="pipeline-editor-run-prompt"]').setValue('test2')
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(pushSpy).toHaveBeenCalledWith({ name: 'run-detail', params: { id: 'run-456' } })
    pushSpy.mockRestore()
    wrapper.unmount()
  })

  // -- handleSaveAsComposite: early return when name is empty --
  it('handleSaveAsComposite returns early when name is empty', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.showSaveAsComposite = true
    vm.saveAsName = ''
    vm.saveAsSelectedNodeIds = ['node-1']

    const callsBefore = vi.mocked(api.POST).mock.calls.length
    await vm.handleSaveAsComposite()
    expect(vi.mocked(api.POST).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- handleSaveAsComposite: early return when no nodes selected --
  it('handleSaveAsComposite returns early when no nodes selected', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.showSaveAsComposite = true
    vm.saveAsName = 'My Composite'
    vm.saveAsSelectedNodeIds = []

    const callsBefore = vi.mocked(api.POST).mock.calls.length
    await vm.handleSaveAsComposite()
    expect(vi.mocked(api.POST).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- convertToAgent: early return when canConvert is false --
  it('convertToAgent returns early when canConvert is false', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-manual', node_type: 'manual', label: 'M', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-manual', type: 'manual', data: { label: 'M', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-manual' } })
    await nextTick()

    // canConvert is false (no agent/connector selected)
    vm.pickerAgentId = '__all__'
    vm.pickerConnectorId = '__all__'

    const callsBefore = vi.mocked(api.POST).mock.calls.length
    await vm.convertToAgent()
    expect(vi.mocked(api.POST).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- revertToManual: early return when snapshot is __all__ --
  it('revertToManual returns early when snapshot is __all__', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vm.revertSnapshotId = '__all__'
    const callsBefore = vi.mocked(api.POST).mock.calls.length
    await vm.revertToManual()
    expect(vi.mocked(api.POST).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- revertToManual: error path sets revertError --
  it('revertToManual error sets revertError', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()
    vm.revertSnapshotId = 'snap-1'

    ;(api.POST as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('revert_failed'))
    await vm.revertToManual()
    await flushPromises()
    expect(vm.revertError).toContain('revert_failed')
    expect(vm.revertLoading).toBe(false)
    wrapper.unmount()
  })

  // -- syncNodeToFlow: early return when selectedNodeData is null --
  it('syncNodeToFlow returns early when selectedNodeData is null', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedNodeData = null
    // should not throw
    vm.syncNodeToFlow()
    wrapper.unmount()
  })

  // -- saveGraph: success clears error and reloads --
  it('saveGraph success clears saveGraphError', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'n1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]
    vm.saveGraphError = 'old_error'

    await vm.saveGraph()
    await flushPromises()

    expect(vm.saveGraphError).toBeNull()
    expect(vm.savingGraph).toBe(false)
    wrapper.unmount()
  })

  // -- saveEdgeConfig: early return when selectedEdgeData is null --
  it('saveEdgeConfig returns early when selectedEdgeData is null', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null

    const callsBefore = vi.mocked(api.PATCH).mock.calls.length
    await vm.saveEdgeConfig()
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- retry policy: toggle, close, keydown handler --
  it('retry policy toggle opens and closes the panel with focus management', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { retry_policy: null }

    // open
    await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()
    expect(vm.retryPolicyOpen).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]').exists()).toBe(true)

    // close via closeRetryPolicy
    vm.closeRetryPolicy()
    await nextTick()
    expect(vm.retryPolicyOpen).toBe(false)

    // open again
    await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()
    expect(vm.retryPolicyOpen).toBe(true)

    wrapper.unmount()
  })

  // -- syncRetryPolicyFromPipeline: non-object retry_policy --
  it('syncRetryPolicyFromPipeline resets to defaults when retry_policy is non-object', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // null policy
    vm.pipeline = { retry_policy: null }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyMode).toBe('all')
    expect(vm.retryPolicyMaxRetries).toBe(0)
    expect(vm.retryPolicyDelaySeconds).toBe(45)
    expect(vm.retryPolicyMultiplier).toBe(2)

    // array policy (malformed)
    vm.pipeline = { retry_policy: ['bad'] }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyMode).toBe('all')

    wrapper.unmount()
  })

  // -- syncRetryPolicyFromPipeline: malformed on (non-list) --
  it('syncRetryPolicyFromPipeline handles malformed on field', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: { on: 'not-an-array', max_retries: 2 } }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyMode).toBe('specific')
    expect(vm.retryPolicyEvents).toEqual([])
    wrapper.unmount()
  })

  // -- syncRetryPolicyFromPipeline: schedule with undefined multiplier --
  it('syncRetryPolicyFromPipeline defaults multiplier to 2 when undefined', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 1, backoff_schedule: { delay_seconds: 60 } } }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyDelaySeconds).toBe(60)
    expect(vm.retryPolicyMultiplier).toBe(2)
    wrapper.unmount()
  })

  // -- retryPolicyNoRetriesWarning: zero events in specific mode --
  it('retryPolicyNoRetriesWarning shows events warning in specific mode with no events', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.retryPolicyMode = 'specific'
    vm.retryPolicyEvents = []
    vm.retryPolicyMaxRetries = 3
    expect(vm.retryPolicyNoRetriesWarning).toContain('No events selected')

    vm.retryPolicyMode = 'specific'
    vm.retryPolicyEvents = ['failure']
    vm.retryPolicyMaxRetries = 0
    expect(vm.retryPolicyNoRetriesWarning).toContain('Max retries')

    vm.retryPolicyMode = 'all'
    vm.retryPolicyMaxRetries = 0
    expect(vm.retryPolicyNoRetriesWarning).toContain('Max retries')

    vm.retryPolicyMode = 'all'
    vm.retryPolicyMaxRetries = 2
    expect(vm.retryPolicyNoRetriesWarning).toBeNull()

    wrapper.unmount()
  })

  // -- agentParamSchema / agentParamSchemaName / paramDefByKey / paramDefLabel --
  it('agentParamSchema, agentParamSchemaName, paramDefByKey, paramDefLabel resolve correctly', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.agents = [{
      id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1',
    }]
    vm.paramSchemas = [{
      id: 'ps-1', name: 'Temp Schema',
      parameters: [{ name: 'temperature', label: 'Temperature', type: 'number' }],
    }]
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // agentParamSchema returns the schema
    const schema = vm.agentParamSchema('agent-1')
    expect(schema).toBeTruthy()
    expect(schema.name).toBe('Temp Schema')

    // no schema for unknown agent
    expect(vm.agentParamSchema('agent-unknown')).toBeUndefined()

    // agentParamSchemaName
    expect(vm.agentParamSchemaName('agent-1')).toBe('Temp Schema')
    expect(vm.agentParamSchemaName('agent-unknown')).toBeUndefined()

    // paramDefByKey
    const def = vm.paramDefByKey('temperature')
    expect(def).toBeTruthy()
    expect(def.type).toBe('number')

    // paramDefLabel
    expect(vm.paramDefLabel('temperature')).toBe('Temperature')

    // paramDefLabel fallback when no label
    vm.paramSchemas = [{ id: 'ps-1', parameters: [{ name: 'foo', type: 'string' }] }]
    expect(vm.paramDefLabel('foo')).toBe('foo')
    expect(vm.paramDefLabel('unknown')).toBe('unknown')

    wrapper.unmount()
  })

  // -- availableParamSets computed --
  it('availableParamSets filters sets by schema', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.paramSets = [
      { id: 'set-1', parameter_schema_id: 'ps-1', name: 'Creative' },
      { id: 'set-2', parameter_schema_id: 'ps-other', name: 'Different' },
    ]
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    expect(vm.availableParamSets.length).toBe(1)
    expect(vm.availableParamSets[0].id).toBe('set-1')
    wrapper.unmount()
  })

  // -- paramSetOverridesKeys computed --
  it('paramSetOverridesKeys returns keys of selectedNodeOverrides', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedNodeOverrides = { temperature: 0.5, top_p: 0.9 }
    expect(vm.paramSetOverridesKeys).toEqual(['temperature', 'top_p'])
    wrapper.unmount()
  })

  // -- openSaveAsComposite: preselects all nodes --
  it('openSaveAsComposite preselects all raw node IDs', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'n1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } },
      { id: 'n2', node_type: 'manual', label: 'B', description: '', position: { x: 100, y: 0 } },
    ]
    await nextTick()

    vm.openSaveAsComposite()
    expect(vm.showSaveAsComposite).toBe(true)
    expect(vm.saveAsSelectedNodeIds).toEqual(['n1', 'n2'])
    expect(vm.saveAsName).toBe('')
    expect(vm.saveAsDescription).toBe('')
    expect(vm.saveAsError).toBeNull()
    wrapper.unmount()
  })

  // -- openAgentPicker / openRevertDialog: resets state --
  it('openAgentPicker and openRevertDialog reset their state', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.openAgentPicker()
    expect(vm.showAgentPicker).toBe(true)
    expect(vm.convertError).toBeNull()
    expect(vm.pickerAgentId).toBe('__all__')
    expect(vm.pickerConnectorId).toBe('__all__')

    vm.openRevertDialog()
    expect(vm.showRevertDialog).toBe(true)
    expect(vm.revertError).toBeNull()
    expect(vm.revertSnapshotId).toBe('__all__')
    wrapper.unmount()
  })

  // -- openRenameDialog: populates from pipeline --
  it('openRenameDialog populates name from pipeline', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { name: 'My Pipeline' }

    vm.openRenameDialog()
    expect(vm.showRenameDialog).toBe(true)
    expect(vm.renameName).toBe('My Pipeline')
    expect(vm.renameError).toBeNull()

    vm.pipeline = null
    vm.openRenameDialog()
    expect(vm.renameName).toBe('')
    wrapper.unmount()
  })

  // -- openRunDialog: resets state --
  it('openRunDialog resets all run dialog state', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.runPrompt = 'old prompt'
    vm.runError = 'old error'
    vm.confirmEmptyRun = true
    vm.emptyRunWarning = 'old warning'

    vm.openRunDialog()
    expect(vm.showRunDialog).toBe(true)
    expect(vm.runPrompt).toBe('')
    expect(vm.runError).toBeNull()
    expect(vm.confirmEmptyRun).toBe(false)
    expect(vm.emptyRunWarning).toBeNull()
    wrapper.unmount()
  })

  // -- closeRunDialog: resets state --
  it('closeRunDialog resets all run dialog state', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.showRunDialog = true
    vm.runPrompt = 'prompt'
    vm.runError = 'error'
    vm.confirmEmptyRun = true
    vm.emptyRunWarning = 'warning'

    vm.closeRunDialog()
    expect(vm.showRunDialog).toBe(false)
    expect(vm.runPrompt).toBe('')
    expect(vm.runError).toBeNull()
    expect(vm.confirmEmptyRun).toBe(false)
    expect(vm.emptyRunWarning).toBeNull()
    wrapper.unmount()
  })

  // -- runPrompt watcher: clears confirmEmptyRun --
  it('runPrompt watcher clears confirmEmptyRun when prompt changes', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.confirmEmptyRun = true
    vm.emptyRunWarning = 'warning'
    vm.runPrompt = 'new text'
    await nextTick()
    expect(vm.confirmEmptyRun).toBe(false)
    expect(vm.emptyRunWarning).toBeNull()
    wrapper.unmount()
  })

  // -- handleRename: early return when name is whitespace --
  it('handleRename returns early when name is whitespace only', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.renameName = '   '
    vm.showRenameDialog = true

    const callsBefore = vi.mocked(api.PATCH).mock.calls.length
    await vm.handleRename()
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- updateMaxDuration: success updates pipeline --
  it('updateMaxDuration updates pipeline.max_duration_seconds on success', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }
    vm.maxDurationInput = 120

    await vm.updateMaxDuration()
    await flushPromises()
    expect(vm.pipeline.max_duration_seconds).toBe(120)
    expect(vm.saveGraphError).toBeNull()
    wrapper.unmount()
  })

  // -- updateMaxDuration: zero sends undefined --
  it('updateMaxDuration sends undefined when value is zero', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }
    vm.maxDurationInput = 0

    await vm.updateMaxDuration()
    await flushPromises()
    const patch = vi.mocked(api.PATCH).mock.calls.find(c => 'max_duration_seconds' in ((c[1] as any).body ?? {}))
    expect(patch).toBeTruthy()
    expect((patch as any)[1].body.max_duration_seconds).toBeUndefined()
    wrapper.unmount()
  })

  // -- retry policy save: with legacy backoff --
  it('saveRetryPolicy preserves legacy backoff when set', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 2, backoff: 10 } }
    vm.syncRetryPolicyFromPipeline()
    await nextTick()

    await vm.saveRetryPolicy()
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls.find(c => String(c[0]).includes('pipelines'))
    expect(patch).toBeTruthy()
    const body = (patch as any)[1].body
    expect(body.retry_policy.backoff).toBe(10)
    expect(body.retry_policy.on).toEqual(['failure'])
    wrapper.unmount()
  })

  // -- retry policy save: without legacy backoff --
  it('saveRetryPolicy omits backoff key when not present', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 2 } }
    vm.syncRetryPolicyFromPipeline()
    await nextTick()

    await vm.saveRetryPolicy()
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls.find(c => String(c[0]).includes('pipelines'))
    expect(patch).toBeTruthy()
    const body = (patch as any)[1].body
    expect(body.retry_policy).not.toHaveProperty('backoff')
    wrapper.unmount()
  })

  // -- nodeTypeOptions: only agent available --
  it('nodeTypeOptions only contains agent', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    expect(vm.nodeTypeOptions.length).toBe(1)
    expect(vm.nodeTypeOptions[0].value).toBe('agent')
    wrapper.unmount()
  })

  // -- handleUnarchive: success sets pipeline --
  it('handleUnarchive sets pipeline on success', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', archived_at: '2026-01-01' }

    useApiFns.post.mockResolvedValueOnce({ id: 'test-pipeline-id', archived_at: null })
    await vm.handleUnarchive()
    await flushPromises()
    expect(vm.pipeline.archived_at).toBeNull()
    wrapper.unmount()
  })

  // -- convertBackendNode: default to 'agent' for unknown types --
  it('convertBackendNode defaults unknown node_type to agent', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const node = vm.convertBackendNode({ id: 'x1', node_type: 'unknown_type', label: 'X', description: '', position: { x: 0, y: 0 } })
    expect(node.type).toBe('agent')
    wrapper.unmount()
  })

  // -- saveGraph: with selectedNodeData param sync --
  it('saveGraph syncs param set and overrides into selectedNodeData', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'n1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } }]
    vm.flowNodes = [{ id: 'n1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'n1' } })
    await nextTick()

    vm.selectedNodeParamSetId = 'ps-1'
    vm.selectedNodeOverrides = { temp: 0.7 }
    await vm.saveGraph()
    await flushPromises()

    expect(vm.selectedNodeData.parameter_set_id).toBe('ps-1')
    expect(vm.selectedNodeData.parameter_overrides).toEqual({ temp: 0.7 })
    wrapper.unmount()
  })

  // -- saveGraph: empty overrides sends null --
  it('saveGraph sends null parameter_overrides when empty', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'n1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 }, parameter_set_id: 'old' }]
    vm.flowNodes = [{ id: 'n1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'n1' } })
    await nextTick()

    vm.selectedNodeParamSetId = undefined
    vm.selectedNodeOverrides = {}
    await vm.saveGraph()
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedNode = (patch[1] as any).body.nodes[0]
    expect(savedNode.parameter_set_id).toBeNull()
    expect(savedNode.parameter_overrides).toBeNull()
    wrapper.unmount()
  })

  // -- saveGraph: savingGraph flag --
  it('saveGraph sets and clears savingGraph flag', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    expect(vm.savingGraph).toBe(false)

    await vm.saveGraph()
    await flushPromises()
    expect(vm.savingGraph).toBe(false)
    wrapper.unmount()
  })

  // -- saveAsNewParamSet: prompt returns empty string --
  it('saveAsNewParamSet returns early when prompt returns empty string', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vi.stubGlobal('prompt', vi.fn().mockReturnValue('   '))
    const callsBefore = vi.mocked(api.POST).mock.calls.length
    await vm.saveAsNewParamSet()
    vi.unstubAllGlobals()
    expect(vi.mocked(api.POST).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  // -- saveAsNewParamSet: POST returns error --
  it('saveAsNewParamSet handles POST error response', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vi.stubGlobal('prompt', vi.fn().mockReturnValue('New Set'))
    ;(api.POST as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ error: { detail: 'schema_error' } })
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await vm.saveAsNewParamSet()
    vi.unstubAllGlobals()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- saveAsNewParamSet: POST throws --
  it('saveAsNewParamSet handles POST exception', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vi.stubGlobal('prompt', vi.fn().mockReturnValue('New Set'))
    ;(api.POST as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('network'))
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await vm.saveAsNewParamSet()
    vi.unstubAllGlobals()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- loadParamSets: error branch --
  it('loadParamSets catches errors and keeps empty array', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    ;(api.GET as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('sets_down'))
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await vm.loadParamSets()
    await flushPromises()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- onParamSetChange: selecting a set populates overrides from paramSets --
  it('onParamSetChange populates overrides from paramSets when set exists', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.paramSets = [{ id: 'set-1', parameter_schema_id: 'ps-1', name: 'Creative', values: { temperature: 0.9, top_p: 0.95 } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vm.selectedNodeParamSetId = 'set-1'
    vm.onParamSetChange()
    expect(vm.selectedNodeOverrides).toEqual({ temperature: 0.9, top_p: 0.95 })
    expect(vm.selectedNodeData.parameter_set_id).toBe('set-1')
    expect(vm.selectedNodeData.parameter_overrides).toEqual({ temperature: 0.9, top_p: 0.95 })

    // deselect
    vm.selectedNodeParamSetId = undefined
    vm.onParamSetChange()
    expect(vm.selectedNodeOverrides).toEqual({})
    wrapper.unmount()
  })

  // -- saveGraph: pipeline loading path --
  it('loadPipeline sets pipeline and syncs retry policy', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementationOnce((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}') && !url.includes('/graph') && !url.includes('/snapshots')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Loaded', max_duration_seconds: 300, retry_policy: { on: ['failure'], max_retries: 3 } }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    await vm.loadPipeline()
    await flushPromises()
    expect(vm.pipeline.name).toBe('Loaded')
    expect(vm.maxDurationInput).toBe(300)
    expect(vm.retryPolicyMaxRetries).toBe(3)
    wrapper.unmount()
  })

  // -- loadPipeline: error path --
  it('loadPipeline error sets pageError', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementationOnce(() => {
      return Promise.reject(new Error('pipeline_not_found'))
    })

    await vm.loadPipeline()
    await flushPromises()
    expect(vm.pageError).toContain('pipeline_not_found')
    wrapper.unmount()
  })

  // -- loadPipeline: no max_duration_seconds --
  it('loadPipeline sets maxDurationInput to undefined when field is absent', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementationOnce((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}') && !url.includes('/graph') && !url.includes('/snapshots')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Loaded' }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    await vm.loadPipeline()
    await flushPromises()
    expect(vm.maxDurationInput).toBeUndefined()
    wrapper.unmount()
  })

  // -- closeRunDialog called by Escape keydown handler --
  it('onRunDialogKeydown closes the dialog on Escape', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.showRunDialog = true

    vm.onRunDialogKeydown(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(vm.showRunDialog).toBe(false)

    // non-Escape key: no effect
    vm.showRunDialog = true
    vm.onRunDialogKeydown(new KeyboardEvent('keydown', { key: 'a' }))
    expect(vm.showRunDialog).toBe(true)
    wrapper.unmount()
  })

  // -- onRetryPolicyKeydown: non-Escape key does nothing --
  it('onRetryPolicyKeydown ignores non-Escape keys', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.retryPolicyOpen = true

    vm.onRetryPolicyKeydown(new KeyboardEvent('keydown', { key: 'a' }))
    expect(vm.retryPolicyOpen).toBe(true)
    wrapper.unmount()
  })

  // -- syncNodeToFlow: no flow node found --
  it('syncNodeToFlow does nothing when flow node is not found', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [] // no flow nodes
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vm.selectedNodeData.label = 'Changed'
    vm.syncNodeToFlow()
    // should not throw, flowNodes still empty
    expect(vm.flowNodes.length).toBe(0)
    wrapper.unmount()
  })

  // -- nodeCommandFields: non-sandbox with commands returns empty --
  it('nodeCommandFields returns empty for agent type with commands', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    expect(vm.nodeCommandFields({ node_type: 'agent', agent_commands: ['cmd'] })).toEqual({})
    expect(vm.nodeCommandFields({ node_type: 'manual', agent_commands: null })).toEqual({})
    wrapper.unmount()
  })

  // -- nodeCommandFields: sandbox with non-string entries in commands array --
  it('nodeCommandFields coerces non-string command entries to strings', async () => {
    router.push('/pipipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const result = vm.nodeCommandFields({
      node_type: 'sandbox_agent',
      agent_commands: [123, null, undefined, 'valid'],
      commands_concatenation_string: ' && ',
    })
    // null/undefined coerce to '' then get filtered by the empty-string filter
    expect(result.agent_commands).toEqual(['123', 'valid'])
    expect(result.commands_concatenation_string).toBe(' && ')
    wrapper.unmount()
  })

  // -- nodeCommandFields: sandbox with joiner fallback --
  it('nodeCommandFields falls back joiner to default when empty string', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const result = vm.nodeCommandFields({
      node_type: 'sandbox_agent',
      agent_commands: ['cmd-a'],
      commands_concatenation_string: '',
    })
    expect(result.commands_concatenation_string).toBe(' && ')
    wrapper.unmount()
  })

  // -- nodeCommandFields: sandbox with no commands at all --
  it('nodeCommandFields returns null for sandbox with no commands', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const result = vm.nodeCommandFields({
      node_type: 'sandbox_agent',
      agent_commands: null,
      commands_concatenation_string: ' && ',
    })
    expect(result.agent_commands).toBeNull()
    expect(result.commands_concatenation_string).toBeNull()
    wrapper.unmount()
  })

  // -- saveGraph: saveGraphError set on failure --
  it('saveGraph sets saveGraphError on failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'n1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('save_failed'))
    await vm.saveGraph()
    await flushPromises()
    expect(vm.saveGraphError).toContain('save_failed')
    expect(vm.savingGraph).toBe(false)
    wrapper.unmount()
  })

  // -- saveEdgeConfig: saving edge flag --
  it('saveEdgeConfig sets and clears savingEdge flag', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawEdges = [
      { id: 'edge-1', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'normal', hitl_gate_config: null, condition_expression: null },
    ]
    vm.flowEdges = [{
      id: 'edge-1', source: 'n1', target: 'n2',
      data: { hitl_gate_config: null, edge_type: 'normal' },
    }]
    vm.onEdgeClick({ edge: { id: 'edge-1' } })
    await nextTick()

    expect(vm.savingEdge).toBe(false)
    await vm.saveEdgeConfig()
    await flushPromises()
    expect(vm.savingEdge).toBe(false)
    wrapper.unmount()
  })

  // -- saveEdgeConfig: HITL gate too short blocks save --
  it('saveEdgeConfig blocks save when HITL description is too short', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawEdges = [{
      id: 'edge-1', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'normal',
      hitl_gate_config: { label: 'Gate', description: 'short' }, condition_expression: null,
    }]
    vm.flowEdges = [{
      id: 'edge-1', source: 'n1', target: 'n2',
      data: { hitl_gate_config: { label: 'Gate' }, edge_type: 'normal' },
    }]
    vm.onEdgeClick({ edge: { id: 'edge-1' } })
    await nextTick()

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockClear()
    await vm.saveEdgeConfig()
    await flushPromises()
    expect(vm.edgeSaveError).toBeTruthy()
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(0)
    wrapper.unmount()
  })
})
