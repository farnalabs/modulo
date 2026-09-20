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

describe('PipelineEditorView — branch coverage sweep', () => {
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

  // -- saveGraph error path --
  it('saveGraph sets saveGraphError on API failure', async () => {
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

  // -- saveEdgeConfig: hitlDescriptionTooShort blocks save --
  it('saveEdgeConfig blocks save when HITL description is too short', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawEdges = [
      { id: 'e1', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'normal', hitl_gate_config: { label: 'Gate', description: 'Short', claim_expiry_minutes: 15 } },
    ]
    vm.selectedEdgeData = vm.rawEdges[0]
    vm.populateEdgeForm(vm.rawEdges[0])
    vm.edgeForm.hitl_enabled = true
    vm.edgeForm.description = 'Too short'
    await nextTick()

    await vm.saveEdgeConfig()
    await flushPromises()
    expect(vm.edgeSaveError).toBeTruthy()
    expect(vm.savingEdge).toBe(false)
    wrapper.unmount()
  })

  // -- saveEdgeConfig error path --
  it('saveEdgeConfig sets edgeSaveError on API failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawEdges = [
      { id: 'e1', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'normal', hitl_gate_config: null },
    ]
    vm.selectedEdgeData = vm.rawEdges[0]
    vm.populateEdgeForm(vm.rawEdges[0])
    vm.edgeForm.hitl_enabled = false

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('edge_save_failed'))
    await vm.saveEdgeConfig()
    await flushPromises()
    expect(vm.edgeSaveError).toContain('edge_save_failed')
    wrapper.unmount()
  })

  // -- triggerRun: empty prompt warning path --
  it('triggerRun shows empty run warning on first empty submit', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]
    vm.showRunDialog = true
    vm.runPrompt = ''

    await vm.triggerRun()
    expect(vm.confirmEmptyRun).toBe(true)
    expect(vm.emptyRunWarning).toBeTruthy()
    wrapper.unmount()
  })

  // -- triggerRun: saveGraph error blocks run --
  it('triggerRun sets runError when saveGraph fails', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]
    vm.showRunDialog = true
    vm.runPrompt = 'test'
    vm.confirmEmptyRun = true

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('graph_save_fail'))
    await vm.triggerRun()
    await flushPromises()
    expect(vm.runError).toBeTruthy()
    wrapper.unmount()
  })

  // -- triggerRun: API error path --
  it('triggerRun sets runError on run API failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]
    vm.showRunDialog = true
    vm.runPrompt = 'test'
    vm.confirmEmptyRun = true

    let patchCount = 0
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockImplementation(() => {
      patchCount++
      if (patchCount === 1) return Promise.resolve({ data: {}, error: undefined }) // saveGraph
      return Promise.resolve({ data: {}, error: undefined })
    })
    ;(api.POST as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('run_api_fail'))
    await vm.triggerRun()
    await flushPromises()
    expect(vm.runError).toContain('run_api_fail')
    wrapper.unmount()
  })

  // -- handleRename error path --
  it('handleRename sets renameError on API failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.renameName = 'New Name'
    vm.showRenameDialog = true

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('rename_fail'))
    await vm.handleRename()
    await flushPromises()
    expect(vm.renameError).toContain('rename_fail')
    expect(vm.renaming).toBe(false)
    wrapper.unmount()
  })

  // -- handleArchive error path --
  it('handleArchive sets pageError on failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }

    useApiFns.post.mockRejectedValueOnce(new Error('archive_fail'))
    await vm.handleArchive()
    await flushPromises()
    expect(vm.pageError).toContain('archive_fail')
    wrapper.unmount()
  })

  // -- handleUnarchive error path --
  it('handleUnarchive sets pageError on failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test', archived_at: '2026-01-01' }

    useApiFns.post.mockRejectedValueOnce(new Error('unarchive_fail'))
    await vm.handleUnarchive()
    await flushPromises()
    expect(vm.pageError).toContain('unarchive_fail')
    wrapper.unmount()
  })

  // -- handleDelete error path --
  it('handleDelete sets deleteError on API failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.showDeleteConfirm = true

    ;(api.DELETE as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('delete_fail'))
    await vm.handleDelete()
    await flushPromises()
    expect(vm.deleteError).toContain('delete_fail')
    wrapper.unmount()
  })

  // -- updateMaxDuration error path --
  it('updateMaxDuration sets saveGraphError on API failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test' }
    vm.maxDurationInput = 120

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('duration_fail'))
    await vm.updateMaxDuration()
    await flushPromises()
    expect(vm.saveGraphError).toContain('duration_fail')
    wrapper.unmount()
  })

  // -- loadGraph: graphError path --
  it('loadGraph sets pageError on graph API error', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: null, error: { detail: 'graph_api_error' } })
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
    expect(vm.pageError).toBeTruthy()
    wrapper.unmount()
  })

  // -- loadGraph: null data path --
  it('loadGraph clears nodes when data is null', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: null, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'old' }]
    await vm.loadGraph()
    await flushPromises()
    expect(vm.rawNodes).toEqual([])
    expect(vm.flowNodes).toEqual([])
    wrapper.unmount()
  })

  // -- loadPipeline error path --
  it('loadPipeline sets pageError on API failure', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/api/v1/pipelines/{pipeline_id}') && !url.includes('graph') && !url.includes('snapshots')) {
        return Promise.reject(new Error('pipeline_load_fail'))
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    await vm.loadPipeline()
    await flushPromises()
    expect(vm.pageError).toContain('pipeline_load_fail')
    wrapper.unmount()
  })

  // -- convertBackendEdge: loop, llm, and default branches --
  it('convertBackendEdge returns correct styles for loop, llm, and normal edges', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const loopEdge = vm.convertBackendEdge({ id: 'e1', edge_type: 'loop', max_iterations: 3, source_node_id: 'n1', target_node_id: 'n2' }, 0)
    expect(loopEdge.style.stroke).toBe('#3b82f6')
    expect(loopEdge.style.strokeDasharray).toBe('5,5')
    expect(loopEdge.animated).toBe(true)

    const llmEdge = vm.convertBackendEdge({ id: 'e2', edge_type: 'llm', routing_label: 'go', source_node_id: 'n1', target_node_id: 'n2' }, 1)
    expect(llmEdge.style.stroke).toBe('#8b5cf6')
    expect(llmEdge.data.routing_label).toBe('go')

    const normalEdge = vm.convertBackendEdge({ id: 'e3', edge_type: 'normal', source_node_id: 'n1', target_node_id: 'n2' }, 2)
    expect(normalEdge.style.stroke).toBe('#888')
    expect(normalEdge.animated).toBe(false)

    wrapper.unmount()
  })

  // -- nodeCommandFields: sandbox_agent with commands --
  it('nodeCommandFields serialises sandbox_agent commands and joiner', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // sandbox_agent with commands
    const result = vm.nodeCommandFields({ node_type: 'sandbox_agent', agent_commands: ['cmd1', 'cmd2'], commands_concatenation_string: ' || ' })
    expect(result.agent_commands).toEqual(['cmd1', 'cmd2'])
    expect(result.commands_concatenation_string).toBe(' || ')

    // sandbox_agent with empty commands
    const emptyResult = vm.nodeCommandFields({ node_type: 'sandbox_agent', agent_commands: [], commands_concatenation_string: null })
    expect(emptyResult.agent_commands).toBeNull()
    expect(emptyResult.commands_concatenation_string).toBeNull()

    // sandbox_agent with default joiner
    const defaultJoiner = vm.nodeCommandFields({ node_type: 'sandbox_agent', agent_commands: ['cmd1'] })
    expect(defaultJoiner.commands_concatenation_string).toBe(' && ')

    // non-sandbox node returns empty
    const nonSandbox = vm.nodeCommandFields({ node_type: 'agent', agent_commands: ['cmd1'] })
    expect(nonSandbox).toEqual({})

    wrapper.unmount()
  })

  // -- findLegacyHitlDescriptionIssues: node + edge paths --
  it('findLegacyHitlDescriptionIssues detects short descriptions on nodes and edges', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const nodes = [
      { id: 'h1', node_type: 'hitl', hitl_config: { description: 'Short' } },
      { id: 'h2', node_type: 'hitl', hitl_config: { description: 'This is a long enough description for the gate' } },
      { id: 'h3', node_type: 'agent', hitl_config: null },
    ]
    const edges = [
      { id: 'e1', source_node_id: 'n1', target_node_id: 'n2', hitl_gate_config: { label: 'Gate', description: 'Short' } },
      { id: 'e2', source_node_id: 'h1', target_node_id: 'n2', hitl_gate_config: { label: 'Gate', description: 'Short' } }, // from hitl node — should be skipped
      { id: 'e3', source_node_id: 'n3', target_node_id: 'n4', hitl_gate_config: { label: 'Gate', description: 'This is a long enough description for the gate' } },
    ]

    const issues = vm.findLegacyHitlDescriptionIssues(nodes, edges)
    expect(issues.some((i: any) => i.kind === 'node')).toBe(true)
    expect(issues.some((i: any) => i.kind === 'edge' && i.key === 'edge:e1')).toBe(true)
    // e2 is from a hitl node, should be skipped (not double-listed)
    expect(issues.some((i: any) => i.kind === 'edge' && i.key === 'edge:e2')).toBe(false)
    wrapper.unmount()
  })

  // -- syncRetryPolicyFromPipeline: schedule with out-of-range values --
  it('syncRetryPolicyFromPipeline clamps out-of-range schedule values', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // delay_seconds out of range (too high)
    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 2, backoff_schedule: { delay_seconds: 500, multiplier: 2 } } }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyDelaySeconds).toBe(300)
    expect(vm.retryPolicyScheduleWarning).toBeTruthy()

    // multiplier out of range (too low)
    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 2, backoff_schedule: { delay_seconds: 45, multiplier: 0.5 } } }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyMultiplier).toBe(1)
    expect(vm.retryPolicyScheduleWarning).toBeTruthy()

    // Non-finite values
    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 2, backoff_schedule: { delay_seconds: NaN, multiplier: Infinity } } }
    vm.syncRetryPolicyFromPipeline()
    expect(vm.retryPolicyDelaySeconds).toBe(45)
    expect(vm.retryPolicyMultiplier).toBe(2)

    wrapper.unmount()
  })

  // -- saveRetryPolicy: granular with zero events blocked --
  it('saveRetryPolicy blocks when specific mode with zero events', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.retryPolicyMode = 'specific'
    vm.retryPolicyEvents = []
    vm.retryPolicyMaxRetries = 3

    await vm.saveRetryPolicy()
    expect(vm.retryPolicyError).toBeTruthy()
    wrapper.unmount()
  })

  // -- saveRetryPolicy: granular with events but zero max blocked --
  it('saveRetryPolicy blocks when specific mode with events but zero max', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.retryPolicyMode = 'specific'
    vm.retryPolicyEvents = ['failure']
    vm.retryPolicyMaxRetries = 0

    await vm.saveRetryPolicy()
    expect(vm.retryPolicyError).toBeTruthy()
    wrapper.unmount()
  })

  // -- saveRetryPolicy: error on API --
  it('saveRetryPolicy sets retryPolicyError on API failure', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { retry_policy: { on: ['failure'], max_retries: 2 } }
    vm.syncRetryPolicyFromPipeline()
    await nextTick()

    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('retry_save_fail'))
    await vm.saveRetryPolicy()
    await flushPromises()
    expect(vm.retryPolicyError).toContain('retry_save_fail')
    expect(vm.retryPolicySaving).toBe(false)
    wrapper.unmount()
  })

  // -- onParamSetChange: selecting a set populates overrides --
  it('onParamSetChange populates overrides from selected set', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.paramSets = [
      { id: 'set-1', parameter_schema_id: 'ps-1', name: 'Creative', values: { temperature: 0.9 } },
    ]
    vm.selectedNodeParamSetId = 'set-1'
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }

    vm.onParamSetChange()
    expect(vm.selectedNodeOverrides).toEqual({ temperature: 0.9 })
    expect(vm.selectedNodeData.parameter_set_id).toBe('set-1')

    // Clearing the set
    vm.selectedNodeParamSetId = undefined
    vm.onParamSetChange()
    expect(vm.selectedNodeOverrides).toEqual({})
    wrapper.unmount()
  })

  // -- convertBackendNode: all type branches --
  it('convertBackendNode maps manual, router, hitl, and default types', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    expect(vm.convertBackendNode({ id: 'a', node_type: 'manual' }).type).toBe('manual')
    expect(vm.convertBackendNode({ id: 'b', node_type: 'router' }).type).toBe('router')
    expect(vm.convertBackendNode({ id: 'c', node_type: 'hitl' }).type).toBe('hitl')
    expect(vm.convertBackendNode({ id: 'd', node_type: 'agent' }).type).toBe('agent')
    expect(vm.convertBackendNode({ id: 'e', node_type: 'unknown' }).type).toBe('agent')
    wrapper.unmount()
  })

  // -- loadLifecycleMaps: full list with linked maps --
  it('loadLifecycleMaps filters maps linked to current pipeline', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps') && !url.includes('/lm-')) {
        return Promise.resolve({ items: [{ id: 'lm-1' }, { id: 'lm-2' }] })
      }
      if (url.includes('/lifecycle-maps/lm-1')) {
        return Promise.resolve({ id: 'lm-1', name: 'Map One', stages: [{ pipeline_id: 'test-pipeline-id' }] })
      }
      if (url.includes('/lifecycle-maps/lm-2')) {
        return Promise.resolve({ id: 'lm-2', name: 'Map Two', stages: [{ pipeline_id: 'other-pipeline' }] })
      }
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    await vm.loadLifecycleMaps()
    await flushPromises()
    expect(vm.linkedLifecycleMaps.length).toBe(1)
    expect(vm.linkedLifecycleMaps[0].id).toBe('lm-1')
    wrapper.unmount()
  })

  // -- saveAsNewParamSet: success path --
  it('saveAsNewParamSet creates set and reloads', async () => {
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

    vi.stubGlobal('prompt', vi.fn().mockReturnValue('My Set'))
    ;(api.POST as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: { id: 'new-set' }, error: undefined })
    await vm.saveAsNewParamSet()
    vi.unstubAllGlobals()
    expect(vi.mocked(api.POST).mock.calls.some(c => String(c[0]).includes('sets'))).toBe(true)
    wrapper.unmount()
  })

  // -- saveAsNewParamSet: API error path --
  it('saveAsNewParamSet logs error on API failure', async () => {
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

    vi.stubGlobal('prompt', vi.fn().mockReturnValue('My Set'))
    ;(api.POST as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: null, error: { detail: 'create_failed' } })
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await vm.saveAsNewParamSet()
    vi.unstubAllGlobals()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
    wrapper.unmount()
  })

  // -- loadParamSets: non-array response --
  it('loadParamSets handles object response shape', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.agents = [{ id: 'agent-1', name: 'Agent One', parameter_schema_id: 'ps-1' }]
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temp Schema' }]
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/sets')) return Promise.resolve({ data: { items: [{ id: 'set-1', parameter_schema_id: 'ps-1', name: 'Test' }] }, error: undefined })
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    await vm.loadParamSets()
    await flushPromises()
    expect(vm.paramSets.length).toBe(1)
    wrapper.unmount()
  })
})
