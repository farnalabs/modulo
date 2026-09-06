import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

// The real useDataFetch cannot run for this view yet: its fetcher references
// `pageErrorRef` destructured from the same statement, which throws a TDZ on
// mount (characterised in PipelineEditorViewLoad.spec.ts). The mock keeps
// these flow tests possible until that production fix lands. The loading ref
// is exported from the mock so tests can flip the loading branch.
const useDataLoading = vi.hoisted(() => ({ value: false }))
vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  const loadingRef = ref(useDataLoading.value)
  const mod = {
    useDataFetch: () => ({
      loading: loadingRef,
      error: ref(null),
      data: ref(undefined),
      fetched: ref(true),
      load: async () => {},
    }),
    __loadingRef: loadingRef,
  }
  return mod
})

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

vi.mock('../lib/api/client', () => {
  // The api client substitutes path params internally, so the mock sees the
  // templated route (e.g. `/api/v1/pipelines/{pipeline_id}/graph`).
  const get = (url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({
        data: {
          nodes: [
            {
              id: 'node-1',
              node_type: 'agent',
              agent_id: 'agent-1',
              label: 'Agent Node',
              description: '',
              position: { x: 0, y: 0 },
              capability_scope: { allowed_connectors: ['conn-1'], allowed_tools: ['tool-a'], context_scope: ['ctx'] },
            },
          ],
          edges: [],
        },
        error: undefined,
      })
    }
    if (url.includes('/api/v1/agents')) {
      return Promise.resolve({ data: { items: [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }] }, error: undefined })
    }
    if (url.includes('/api/v1/connectors')) {
      return Promise.resolve({ data: { items: [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }] }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
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

async function mountEditorLoaded() {
  const wrapper = mountEditor()
  await flushPromises()
  await nextTick()
  return wrapper
}

// The global test setup mocks vue-router with a static route (params {}). The
// view captures `route.params.id` at setup, so seed it before every mount so
// pipeline-scoped calls carry the real id.
beforeEach(async () => {
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useDataLoading.value = false
    useApiFns.get.mockReset()
    useApiFns.post.mockReset()
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    useApiFns.post.mockResolvedValue({})
  })

  it('renders without crashing', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })

  it('renders the capability scope panel for an agent node and persists edits', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()

    const vm = wrapper.vm as any
    // useDataFetch is mocked out; seed the loader state directly so the panel
    // can be driven deterministically without relying on async graph loading.
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'agent',
        agent_id: 'agent-1',
        label: 'Agent Node',
        description: '',
        position: { x: 0, y: 0 },
        capability_scope: { allowed_connectors: ['conn-1'], allowed_tools: ['tool-a'], context_scope: ['ctx'] },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }]
    vm.connectors = [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()
    expect((wrapper.vm as any).selectedNodeData).toBeTruthy()

    const panel = wrapper.find('[data-testid="pipeline-editor-capability-scope"]')
    expect(panel.exists()).toBe(true)

    // connector checkbox is present and pre-selected from the saved scope
    const connCheckbox = wrapper.find('[data-testid="pipeline-editor-scope-connector-conn-1"]')
    expect(connCheckbox.exists()).toBe(true)
    expect((connCheckbox.element as HTMLInputElement).checked).toBe(true)

    // displayed connector label
    expect(panel.text()).toContain('Slack Dev (slack)')

    // add a free-form tool
    await wrapper.find('[data-testid="pipeline-editor-scope-tool-input"]').setValue('tool-b')
    await wrapper.find('[data-testid="pipeline-editor-scope-tool-add"]').trigger('click')
    await nextTick()
    expect(vm.selectedNodeData.capability_scope.allowed_tools).toContain('tool-b')

    // reset to unrestricted clears scope
    await wrapper.find('[data-testid="pipeline-editor-scope-reset"]').trigger('click')
    await nextTick()
    expect(vm.selectedNodeData.capability_scope).toBeNull()
  })

  it('offers only the backend-supported retry policy events and filters unknown values', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()

    await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()

    const panel = wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]')
    expect(panel.exists()).toBe(true)

    // Only the events the backend allowlist accepts are offered in the UI.
    // eval_failed became backend-supported in FAR-503: the API allowlist
    // (_RETRY_POLICY_EVENTS in api/routes/pipelines.py), the graph validator and
    // the executor's retry matching all accept it in lockstep, so the editor
    // offers it as a selectable event.
    const supportedEvents = ['stall', 'timeout', 'failure', 'eval_failed']
    for (const event of supportedEvents) {
      const checkbox = wrapper.find(`[data-testid="pipeline-editor-retry-event-${event}"]`)
      expect(checkbox.exists(), `retry event checkbox for ${event}`).toBe(true)
    }

    // round-trip: a persisted policy is loaded safely — the allowlist is derived
    // from retryPolicyOptions, so every backend-supported event (including
    // eval_failed) survives a reload while genuinely unknown values are dropped
    // and the editor never crashes on stale payloads.
    ;(wrapper.vm as any).pipeline = {
      retry_policy: { on: ['eval_failed', 'stall', 'bogus_event'], max_retries: 2 },
    }
    ;(wrapper.vm as any).syncRetryPolicyFromPipeline()
    await nextTick()
    expect((wrapper.vm as any).retryPolicyEvents).toEqual(['eval_failed', 'stall'])
    const stallCheckbox = wrapper.find('[data-testid="pipeline-editor-retry-event-stall"]')
    expect((stallCheckbox.element as HTMLInputElement).checked).toBe(true)
  })

  describe('FAR-525 retry backoff schedule round-trip', () => {
    async function mountWithPolicy(retryPolicy: Record<string, unknown>) {
      router.push('/pipelines/test-pipeline-id/editor')
      await router.isReady()
      const wrapper = mountEditor()
      await flushPromises()
      ;(wrapper.vm as any).pipeline = { retry_policy: retryPolicy }
      ;(wrapper.vm as any).syncRetryPolicyFromPipeline()
      await nextTick()
      return wrapper
    }

    function lastPatchBody(): any {
      const calls = vi.mocked(api.PATCH).mock.calls
      expect(calls.length).toBeGreaterThan(0)
      // PATCH(url, { params, body, signal }) — the request body lives on the
      // second argument.
      return (calls[calls.length - 1][1] as any).body
    }

    it('loads backoff_schedule delay/multiplier into the panel and preserves the legacy backoff on save', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 2,
        backoff: 12,
        backoff_schedule: { delay_seconds: 30, multiplier: 1.5 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(30)
      expect(vm.retryPolicyMultiplier).toBe(1.5)

      // change only max_retries, then save: schedule AND legacy backoff survive
      vm.retryPolicyMaxRetries = 3
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: ['failure'],
        max_retries: 3,
        backoff: 12,
        backoff_schedule: { delay_seconds: 30, multiplier: 1.5 },
      })
    })

    it('save in the disable direction sends on: [] and preserves schedule and legacy backoff (not {})', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure', 'stall'],
        max_retries: 2,
        backoff: 7,
        backoff_schedule: { delay_seconds: 90, multiplier: 2 },
      })
      const vm = wrapper.vm as any
      vm.retryPolicyEvents = []
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: [],
        max_retries: 2,
        backoff: 7,
        backoff_schedule: { delay_seconds: 90, multiplier: 2 },
      })
    })

    it('rebuilds backoff_schedule from panel state, dropping junk inner keys (enable direction)', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 2,
        backoff_schedule: { delay_seconds: 45, multiplier: 2, junk_key: 'hand-edited' },
      })
      const vm = wrapper.vm as any
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy.backoff_schedule).toEqual({
        delay_seconds: 45,
        multiplier: 2,
      })
    })

    it('rebuilds backoff_schedule from panel state, dropping junk inner keys (disable direction)', async () => {
      const wrapper = await mountWithPolicy({
        on: ['timeout'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 20, multiplier: 3, junk_key: 'hand-edited' },
      })
      const vm = wrapper.vm as any
      vm.retryPolicyEvents = []
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: [],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 20, multiplier: 3 },
      })
    })

    it('sends the default 45s x 2.0 schedule when no schedule is stored, without a legacy backoff key', async () => {
      const wrapper = await mountWithPolicy({ on: ['failure'], max_retries: 2 })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(45)
      expect(vm.retryPolicyMultiplier).toBe(2)
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: ['failure'],
        max_retries: 2,
        backoff_schedule: { delay_seconds: 45, multiplier: 2 },
      })
    })

    it('clamps out-of-range stored schedule values and surfaces the runtime fail-open warning', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 1000, multiplier: 25 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(300)
      expect(vm.retryPolicyMultiplier).toBe(10)

      // open the panel (toggle re-syncs from the same stored policy) and check
      // the warning states the ACTUAL runtime behaviour: fail-open to default.
      await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
      await nextTick()
      const warning = wrapper.find('[data-testid="pipeline-editor-retry-policy-schedule-warning"]')
      expect(warning.exists()).toBe(true)
      expect(warning.text()).toContain('fails open')
    })

    it('does not warn for in-range or absent schedules', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 60 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(60)
      expect(vm.retryPolicyMultiplier).toBe(2)
      expect(vm.retryPolicyScheduleWarning).toBeNull()

      await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
      await nextTick()
      expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-schedule-warning"]').exists()).toBe(false)
    })
  })

  it('shows the sandbox commands editor for a node with a pre-existing command list', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'sandbox_agent',
        template_id: 'opencode',
        agent_prompt: 'do the thing',
        agent_command: null,
        agent_commands: ['opencode run', '--model oxf'],
        commands_concatenation_string: ' ; ',
        label: 'Sandbox',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // pre-existing rows are legible in the editor (one input per command)
    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-editor"]').exists()).toBe(true)
    const row0 = wrapper.find('[data-testid="pipeline-editor-node-command-row-0"]')
    expect((row0.element as HTMLInputElement).value).toBe('opencode run')
    const row1 = wrapper.find('[data-testid="pipeline-editor-node-command-row-1"]')
    expect((row1.element as HTMLInputElement).value).toBe('--model oxf')
  })

  it('saves the authored command list + join operator on the node config', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    const rawNode = {
      id: 'node-1',
      node_type: 'sandbox_agent',
      template_id: 'opencode',
      agent_prompt: 'do the thing',
      agent_command: 'legacy-scalar',
      agent_commands: ['opencode run', '--model oxf'],
      commands_concatenation_string: ' ; ',
      label: 'Sandbox',
      description: '',
      position: { x: 0, y: 0 },
    }
    vm.rawNodes = [rawNode]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await vm.saveGraph()

    const patchMock = vi.mocked(api.PATCH)
    expect(patchMock).toHaveBeenCalled()
    const savedNode = (patchMock.mock.calls[0][1] as any).body.nodes[0]
    // list + custom joiner survive the save payload (round-trip)
    expect(savedNode.agent_commands).toEqual(['opencode run', '--model oxf'])
    expect(savedNode.commands_concatenation_string).toBe(' ; ')
    // mutual exclusion: the scalar is cleared when a non-empty list is authored
    expect(savedNode.agent_command).toBeNull()
  })

  it('saves a scalar-only sandbox command without inventing a list', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'sandbox_agent',
        template_id: 'opencode',
        agent_prompt: 'do the thing',
        agent_command: 'opencode run --auto',
        label: 'Sandbox',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await vm.saveGraph()

    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    expect(savedNode.agent_command).toBe('opencode run --auto')
    expect(savedNode.agent_commands).toBeNull()
    expect(savedNode.commands_concatenation_string).toBeNull()
  })

  it('falls back the join operator to the default when a list is saved without one', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'sandbox_agent',
        template_id: 'opencode',
        agent_prompt: 'do the thing',
        agent_command: 'legacy-scalar',
        agent_commands: ['opencode run', '--model oxf'],
        label: 'Sandbox',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await vm.saveGraph()

    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    // unset joiner saves as the runtime default; the scalar is cleared by the
    // list's presence (mutual exclusion at the payload boundary too)
    expect(savedNode.commands_concatenation_string).toBe(' && ')
    expect(savedNode.agent_commands).toEqual(['opencode run', '--model oxf'])
    expect(savedNode.agent_command).toBeNull()
  })

  it('filters empty command rows and keeps a scalar-only node intact on save', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'sandbox_agent',
        template_id: 'opencode',
        agent_prompt: 'do the thing',
        agent_command: 'opencode run --auto',
        agent_commands: ['cmd-a', '   ', ''],
        commands_concatenation_string: ' ; ',
        label: 'Sandbox',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await vm.saveGraph()
    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    // empty/whitespace rows are dropped; the remaining list wins over the scalar
    expect(savedNode.agent_commands).toEqual(['cmd-a'])
    expect(savedNode.agent_command).toBeNull()
    expect(savedNode.commands_concatenation_string).toBe(' ; ')
  })

  it('spread-preserves the sandbox node model fields in the save payload (template_id + sandbox config)', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'sandbox_agent',
        template_id: 'opencode',
        mode: 'llm',
        agent_command: 'opencode run --auto',
        agent_commands: null,
        commands_concatenation_string: ' && ',
        agent_prompt: 'do the thing',
        egress_policy: 'selected',
        egress_allowlist: [{ host: 'github.com', port: 443 }],
        resource_limits: { cpu: 2 },
        wallclock_budget_seconds: 600,
        delivery_sentinel: 'DELIVERY_DONE',
        env_vars: { FOO: 'bar' },
        context_files: { '/home/user/notes.txt': 'notes' },
        output_schema_json: { type: 'object' },
        autonomy_recommendation: 'autonomy_low',
        input_schema_pin: { schema_id: 'schema-1', schema_version: 'v1' },
        label: 'Sandbox',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await vm.saveGraph()

    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    // the critical regression: _validate_sandbox_agent_node 422s without a
    // template_id, so a save that drops it bricks every sandbox pipeline edit
    expect(savedNode.template_id).toBe('opencode')
    // the hand-maintained payload map silently dropped the sandbox config
    // surface — every one of these fields must survive the round-trip
    expect(savedNode.egress_policy).toBe('selected')
    expect(savedNode.egress_allowlist).toEqual([{ host: 'github.com', port: 443 }])
    expect(savedNode.resource_limits).toEqual({ cpu: 2 })
    expect(savedNode.wallclock_budget_seconds).toBe(600)
    expect(savedNode.delivery_sentinel).toBe('DELIVERY_DONE')
    expect(savedNode.env_vars).toEqual({ FOO: 'bar' })
    expect(savedNode.context_files).toEqual({ '/home/user/notes.txt': 'notes' })
    expect(savedNode.output_schema_json).toEqual({ type: 'object' })
    expect(savedNode.autonomy_recommendation).toBe('autonomy_low')
    expect(savedNode.input_schema_pin).toEqual({ schema_id: 'schema-1', schema_version: 'v1' })
    // command normalisation still layers on top of the spread
    expect(savedNode.agent_command).toBe('opencode run --auto')
    expect(savedNode.agent_commands).toBeNull()
  })

  it('keeps composite node identity + schema pins in the save payload and omits UI-only keys', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'composite',
        composite_ref: 'composite-1',
        composite_parameter_values: { region: 'eu' },
        composite_input_mapping: { in: 'a' },
        composite_output_mapping: { out: 'b' },
        input_schema_pin: { schema_id: 'schema-1', schema_version: 'v2' },
        label: 'Composite',
        description: '',
        position: { x: 0, y: 0 },
        // UI-state markers that must never leak into the payload
        type: 'agent',
        data: { label: 'Composite' },
        selected: true,
        model_backend_id: 'mb-1',
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Composite', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await vm.saveGraph()

    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    // "Composite nodes require a composite_ref" — dropping it hard-422s the save
    expect(savedNode.composite_ref).toBe('composite-1')
    expect(savedNode.composite_parameter_values).toEqual({ region: 'eu' })
    expect(savedNode.composite_input_mapping).toEqual({ in: 'a' })
    expect(savedNode.composite_output_mapping).toEqual({ out: 'b' })
    expect(savedNode.input_schema_pin).toEqual({ schema_id: 'schema-1', schema_version: 'v2' })
    // view-only keys are stripped, not persisted
    expect(savedNode).not.toHaveProperty('type')
    expect(savedNode).not.toHaveProperty('data')
    expect(savedNode).not.toHaveProperty('selected')
    expect(savedNode).not.toHaveProperty('model_backend_id')
  })

  it('shows commands read-only for an agent node and round-trips its payload without command mutations', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'agent',
        agent_id: 'agent-1',
        agent_command: 'node-level-legacy-command',
        agent_commands: null,
        commands_concatenation_string: ' && ',
        label: 'Agent Node',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // the authoring editor is sandbox-only; the read-only display renders instead
    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-editor"]').exists()).toBe(false)
    const readonly = wrapper.find('[data-testid="pipeline-editor-node-commands-readonly"]')
    expect(readonly.exists()).toBe(true)
    expect(readonly.text()).toContain('node-level-legacy-command')

    await vm.saveGraph()

    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    // the save payload round-trips the stored command verbatim — the editor
    // never fabricates or rewrites commands on a non-sandbox node (FAR-488a
    // syncs a node-level agent_command into the bound Agent's row)
    expect(savedNode.agent_command).toBe('node-level-legacy-command')
    expect(savedNode.agent_commands).toBeNull()
    expect(savedNode.commands_concatenation_string).toBe(' && ')
  })

  it('renders no commands editor for a manual node', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'manual',
        output_schema_id: 'schema-1',
        agent_command: null,
        agent_commands: null,
        label: 'Manual Step',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'manual', data: { label: 'Manual Step', description: '' } }]
    vm.schemas = [{ id: 'schema-1', name: 'Output Schema' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-editor"]').exists()).toBe(false)
    // no command data on the node → no read-only block either
    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-readonly"]').exists()).toBe(false)
  })

  it('labels a sandbox_agent node "Runner" in the node properties panel (ADR 029 vocabulary)', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'sandbox_agent',
        agent_id: 'agent-1',
        agent_commands: [],
        label: 'My Runner Node',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'My Runner Node', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    expect(wrapper.text()).toContain('Runner')
  })

  it('labels an agent node "Inline Prompt" in the node properties panel (ADR 029 vocabulary)', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'agent',
        agent_id: 'agent-1',
        label: 'My Prompt Node',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'My Prompt Node', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    expect(wrapper.text()).toContain('Inline Prompt')
  })
})

describe('PipelineEditorView — toolbar rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useDataLoading.value = false
    useApiFns.get.mockReset()
    useApiFns.post.mockReset()
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    useApiFns.post.mockResolvedValue({})
  })

  it('shows the loading spinner state while the loaders run', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const mod = (await import('../composables/useDataFetch')) as unknown as { __loadingRef: { value: boolean } }
    mod.__loadingRef.value = true
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    mod.__loadingRef.value = false
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders the archived badge with an unarchive button and feature-gated actions', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'My Pipeline', archived_at: '2026-01-01T00:00:00Z' }
    await nextTick()

    const toolbar = wrapper.find('[data-testid="pipeline-editor-toolbar"]')
    expect(toolbar.exists()).toBe(true)
    expect(toolbar.text()).toContain('My Pipeline')
    expect(toolbar.text()).toContain('Archived')
    // archived pipeline shows Unarchive, not Archive
    expect(wrapper.find('[data-testid="pipeline-editor-unarchive"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-archive"]').exists()).toBe(false)
    // plan features enable the Versions + Delete buttons
    expect(wrapper.find('[data-testid="pipeline-editor-version-timeline"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-delete"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('hides feature-gated toolbar buttons when the plan lacks them', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const store = usePlanStore()
    store.features = {}
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-timeline"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-delete"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders the folder breadcrumb for a filed pipeline', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.folders = [{ id: 'f-root', name: 'Root', parent_id: null }, { id: 'f1', name: 'Production', parent_id: 'f-root' }]
    vm.pipeline = { id: 'test-pipeline-id', name: 'My Pipeline', folder_id: 'f1' }
    await nextTick()

    const identity = wrapper.find('[data-testid="pipeline-editor-toolbar-group-identity"]')
    expect(identity.text()).toContain('Root')
    expect(identity.text()).toContain('Production')
    const links = identity.findAll('a').map((a) => a.attributes('href'))
    expect(links.some((h) => h?.includes('folder_id=f1'))).toBe(true)
    wrapper.unmount()
  })

  it('renders linked lifecycle maps in the toolbar', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.linkedLifecycleMaps = [{ id: 'lm-1', name: 'Checkout Flow' }]
    await nextTick()

    expect(wrapper.text()).toContain('Checkout Flow')
    const link = wrapper.findAll('a').find((a) => a.text() === 'Checkout Flow')
    expect(link?.attributes('href')).toContain('/lifecycle-maps/lm-1')
    wrapper.unmount()
  })

  it('shows the empty-state overlay for a graph-less pipeline and adds a node from it', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    ;(wrapper.vm as any).pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    await nextTick()
    expect(wrapper.text()).toContain('Test Pipeline')
    // add node appends to both the flow and the raw graph
    const addBtns = wrapper.findAll('[data-testid="pipeline-editor-add-node"]')
    expect(addBtns.length).toBeGreaterThan(0)
    await addBtns[0].trigger('click')
    await nextTick()
    const vm = wrapper.vm as any
    expect(vm.flowNodes.length).toBe(1)
    expect(vm.rawNodes.length).toBe(1)
    expect(vm.rawNodes[0].node_type).toBe('agent')
    wrapper.unmount()
  })

  it('surfaces a save-graph failure in the toolbar', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('422 validation')))
    await (wrapper.vm as any).saveGraph()
    await flushPromises()
    const err = wrapper.find('[data-testid="pipeline-editor-save-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('422 validation')
    wrapper.unmount()
  })
})

describe('PipelineEditorView — run dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useDataLoading.value = false
    useApiFns.get.mockReset()
    useApiFns.post.mockReset()
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    useApiFns.post.mockResolvedValue({})
  })

  it('opens the run dialog, warns once on an empty prompt, then runs with the prompt', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    // triggerRun guards on the loaded pipeline
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    // the Run button is disabled until the graph has nodes
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'Agent Node', description: '', position: { x: 0, y: 0 } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(true)

    // empty prompt → first click warns, second click proceeds
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No input provided')
    // typing clears the confirm state
    await wrapper.find('[data-testid="pipeline-editor-run-prompt"]').setValue('do the thing')
    await nextTick()
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls.find((c) => c[0] === '/api/v1/runs')
    expect(post).toBeTruthy()
    expect((post as unknown[] | undefined)![1]).toEqual(expect.objectContaining({
      body: { pipeline_id: 'test-pipeline-id', input_payload: { prompt: 'do the thing' } },
    }))
    // dialog closed after the run started
    expect(vm.showRunDialog).toBe(false)
    wrapper.unmount()
  })

  it('renders the webhook info instead of the prompt for webhook-triggered pipelines', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Webhook Pipe', trigger_type: 'webhook' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('webhook')
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-run-submit"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows an inline error when the run POST fails', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'Agent Node', description: '', position: { x: 0, y: 0 } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="pipeline-editor-run-prompt"]').setValue('go')
    ;(api.POST as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('budget_exceeded')))
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('budget_exceeded')
    // dialog stays open
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('closes the run dialog on Escape', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renames the pipeline and reflects the new name', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockImplementationOnce(() =>
      Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Renamed Pipeline' }, error: undefined }))

    await wrapper.find('[data-testid="pipeline-editor-rename"]').trigger('click')
    await nextTick()
    // the FormDialog teleports its content to document.body
    const input = document.querySelector<HTMLInputElement>('#pipelineeditorview-field-1')!
    expect(input.value).toBe('Test Pipeline')
    input.value = 'Renamed Pipeline'
    input.dispatchEvent(new Event('input'))
    await nextTick()
    const confirm = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Save')!
    confirm.click()
    await flushPromises()
    await nextTick()

    const patch = vi.mocked(api.PATCH).mock.calls.find((c) => (c[1] as any).body?.name === 'Renamed Pipeline')
    expect(patch).toBeTruthy()
    expect(vm.pipeline.name).toBe('Renamed Pipeline')
    expect(vm.showRenameDialog).toBe(false)
    wrapper.unmount()
  })

  it('shows a rename failure inline', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-rename"]').trigger('click')
    await nextTick()
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('name_taken')))
    const input = document.querySelector<HTMLInputElement>('#pipelineeditorview-field-1')!
    input.value = 'Bad Name'
    input.dispatchEvent(new Event('input'))
    await nextTick()
    const confirm = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Save')!
    confirm.click()
    await flushPromises()
    await nextTick()

    // the rename error renders inside the teleported dialog
    expect(document.body.textContent).toContain('name_taken')
    wrapper.unmount()
  })

  it('archives and unarchives the pipeline through the useApi post path', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    await nextTick()

    useApiFns.post.mockResolvedValueOnce({ id: 'test-pipeline-id', name: 'Test Pipeline', archived_at: '2026-01-01T00:00:00Z' })
    await wrapper.find('[data-testid="pipeline-editor-archive"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(useApiFns.post).toHaveBeenCalledWith('/api/v1/pipelines/test-pipeline-id/archive')
    expect(wrapper.find('[data-testid="pipeline-editor-unarchive"]').exists()).toBe(true)

    useApiFns.post.mockResolvedValueOnce({ id: 'test-pipeline-id', name: 'Test Pipeline', archived_at: null })
    await wrapper.find('[data-testid="pipeline-editor-unarchive"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-archive"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a page error when archiving fails', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    await nextTick()

    useApiFns.post.mockRejectedValueOnce(new Error('archive_denied'))
    await wrapper.find('[data-testid="pipeline-editor-archive"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Failed to archive pipeline')
    expect(wrapper.text()).toContain('archive_denied')
    wrapper.unmount()
  })

  it('deletes the pipeline after confirmation and navigates to the library', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()

    await wrapper.find('[data-testid="pipeline-editor-delete"]').trigger('click')
    await nextTick()
    // delete dialog teleports to body
    expect(document.body.textContent).toContain('Delete Pipeline')

    const confirm = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Delete')!
    confirm.click()
    await flushPromises()
    await nextTick()

    const del = vi.mocked(api.DELETE).mock.calls[0]
    expect(del[0]).toBe('/api/v1/pipelines/{pipeline_id}')
    wrapper.unmount()
  })

  it('shows a delete failure inline', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()

    ;(api.DELETE as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('delete_denied')))
    await wrapper.find('[data-testid="pipeline-editor-delete"]').trigger('click')
    await nextTick()
    const confirm = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Delete')!
    confirm.click()
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).toContain('delete_denied')
    wrapper.unmount()
  })

  it('persists the max duration setting on change', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()

    await wrapper.find('[data-testid="pipeline-editor-max-duration"]').setValue('120')
    await wrapper.find('[data-testid="pipeline-editor-max-duration"]').trigger('change')
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls.find((c) => (c[1] as any).body?.max_duration_seconds !== undefined)
    expect(patch).toBeTruthy()
    expect((patch as unknown[] | undefined)![1]).toEqual(expect.objectContaining({
      body: { max_duration_seconds: 120 },
    }))
    wrapper.unmount()
  })

  it('sends undefined max duration when the input is cleared or non-positive', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()

    await wrapper.find('[data-testid="pipeline-editor-max-duration"]').setValue('0')
    await wrapper.find('[data-testid="pipeline-editor-max-duration"]').trigger('change')
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls.find((c) => 'max_duration_seconds' in ((c[1] as any).body ?? {}))
    expect(patch).toBeTruthy()
    expect((patch as unknown[] | undefined)![1]).toEqual(expect.objectContaining({
      body: { max_duration_seconds: undefined },
    }))
    wrapper.unmount()
  })

  it('surfaces a max-duration update failure in the toolbar', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()

    // Reset first: earlier tests in this describe queue once-implementations
    // on api.PATCH that would otherwise consume this reject.
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockReset()
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {}, error: undefined })
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('duration_rejected')))
    // setValue already dispatches a change event — the handler runs here.
    await wrapper.find('[data-testid="pipeline-editor-max-duration"]').setValue('99999')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-save-error"]').text()).toContain('Failed to update max duration')
    expect(wrapper.find('[data-testid="pipeline-editor-save-error"]').text()).toContain('duration_rejected')
    wrapper.unmount()
  })
})

describe('PipelineEditorView — edge properties panel', () => {
  function edgeFixture(overrides: Record<string, unknown> = {}) {
    return {
      id: 'edge-1',
      source_node_id: 'node-1',
      target_node_id: 'node-2',
      edge_type: 'normal',
      condition_expression: null,
      hitl_gate_config: null,
      ...overrides,
    }
  }

  async function mountWithEdge(edge: Record<string, unknown>) {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawEdges = [edge]
    vm.flowEdges = [{
      id: edge.id,
      source: edge.source_node_id,
      target: edge.target_node_id,
      data: { hitl_gate_config: edge.hitl_gate_config, edge_type: edge.edge_type },
    }]
    vm.onEdgeClick({ edge: { id: edge.id } })
    await nextTick()
    return wrapper
  }

  beforeEach(() => {
    vi.clearAllMocks()
    useDataLoading.value = false
    useApiFns.get.mockReset()
    useApiFns.post.mockReset()
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    useApiFns.post.mockResolvedValue({})
  })

  it('opens the edge panel with source/target and the HITL gate block populated', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: {
        label: 'Review gate',
        description: 'Human review',
        claim_expiry_minutes: 30,
        human_only: true,
        condition: 'status == "ok"',
      },
    }))

    const panel = wrapper.findAll('aside').find((a) => a.text().includes('Edge Properties'))
    expect(panel).toBeTruthy()

    // HITL fields rendered from the stored gate config (values live on the
    // inputs, not in the text content)
    const labelInput = panel!.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'Review gate')
    expect(labelInput).toBeTruthy()
    const conditionInput = panel!.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'status == "ok"')
    expect(conditionInput).toBeTruthy()
    const claimExpiry = panel!.findAll('input[type="number"]').find((i) => Number((i.element as HTMLInputElement).value) === 30)
    expect(claimExpiry).toBeTruthy()
    wrapper.unmount()
  })

  it('shows the eval condition fields for an eval-condition gate', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: {
        eval_condition: { eval_name: 'quality', threshold: 0.9, operator: 'gte' },
      },
    }))

    const panel = wrapper.findAll('aside').find((a) => a.text().includes('Edge Properties'))
    const evalName = panel!.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'quality')
    expect(evalName).toBeTruthy()
    const threshold = panel!.findAll('input[type="number"]').find((i) => Number((i.element as HTMLInputElement).value) === 0.9)
    expect(threshold).toBeTruthy()
    wrapper.unmount()
  })

  it('shows the max-iterations field for loop edges and the routing label for llm edges', async () => {
    // Edges WITH a HITL gate config keep their form state (see the BUG test
    // below for the gate-less reset).
    const wrapper = await mountWithEdge(edgeFixture({ edge_type: 'loop', max_iterations: 4, hitl_gate_config: { label: 'Gate' } }))
    let panel = wrapper.findAll('aside').find((a) => a.text().includes('Edge Properties'))
    const maxIter = panel!.findAll('input[type="number"]').find((i) => Number((i.element as HTMLInputElement).value) === 4)
    expect(maxIter).toBeTruthy()
    wrapper.unmount()

    const wrapper2 = await mountWithEdge(edgeFixture({ edge_type: 'llm', routing_label: 'escalate', hitl_gate_config: { label: 'Gate' } }))
    panel = wrapper2.findAll('aside').find((a) => a.text().includes('Edge Properties'))
    const routing = panel!.findAll('input').find((i) => (i.element as HTMLInputElement).value === 'escalate')
    expect(routing).toBeTruthy()
    wrapper2.unmount()
  })

  it('BUG: opening a loop edge without a HITL gate resets the form to defaults', async () => {
    // Production bug characterisation. populateEdgeForm() sets edge_type /
    // max_iterations / routing_label from the edge, but its gate-less branch
    // then does `Object.assign(edgeForm, { ...defaultEdgeForm })`, wiping the
    // values it just set: a loop edge with no HITL gate opens as type
    // "normal" with max_iterations 0, and saving the panel would overwrite
    // the edge's type in the graph.
    const wrapper = await mountWithEdge(edgeFixture({ edge_type: 'loop', max_iterations: 4 }))
    const vm = wrapper.vm as any
    expect(vm.selectedEdgeData.edge_type).toBe('loop')
    // the form was reset, losing the edge's actual type and iterations
    expect(vm.edgeForm.edge_type).toBe('normal')
    expect(vm.edgeForm.max_iterations).toBe(0)
    expect(wrapper.findAll('aside').find((a) => a.text().includes('Edge Properties'))!.findAll('input[type="number"]').length).toBe(0)
    wrapper.unmount()
  })

  it('saves the edge config and reloads the graph', async () => {
    const wrapper = await mountWithEdge(edgeFixture({ hitl_gate_config: { label: 'Review gate' } }))

    const saveEdge = wrapper.find('[data-testid="pipeline-editor-save-edge"]')
    await saveEdge.trigger('click')
    await flushPromises()
    await nextTick()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    expect(patch[0]).toBe('/api/v1/pipelines/{pipeline_id}/graph')
    const savedEdge = (patch[1] as any).body.edges[0]
    expect(savedEdge.hitl_gate_config.label).toBe('Review gate')
    expect(savedEdge.edge_type).toBe('normal')
    wrapper.unmount()
  })

  it('shows an edge save failure inline', async () => {
    const wrapper = await mountWithEdge(edgeFixture({ hitl_gate_config: { label: 'Review gate' } }))
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('edge_rejected')))
    await wrapper.find('[data-testid="pipeline-editor-save-edge"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('edge_rejected')
    wrapper.unmount()
  })

  it('clears the selection on a pane click', async () => {
    const wrapper = await mountWithEdge(edgeFixture({ hitl_gate_config: { label: 'Review gate' } }))
    ;(wrapper.vm as any).onPaneClick()
    await nextTick()
    expect(wrapper.findAll('aside').find((a) => a.text().includes('Edge Properties'))).toBeUndefined()
    wrapper.unmount()
  })
})

describe('PipelineEditorView — dialogs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useDataLoading.value = false
    useApiFns.get.mockReset()
    useApiFns.post.mockReset()
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    useApiFns.post.mockResolvedValue({})
  })

  async function mountWithManualNode() {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-manual',
        node_type: 'manual',
        label: 'Manual Step',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-manual', type: 'manual', data: { label: 'Manual Step', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', model_backend_id: 'mb-1', connector_type_refs: [{ connector_type: 'slack' }], input_schema_id: 's-in', output_schema_id: 's-out' }]
    vm.connectors = [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }]
    vm.modelBackends = [{ id: 'mb-1', display_name: 'Claude', provider: 'anthropic' }]
    vm.schemas = [{ id: 's-in', name: 'Input Schema' }, { id: 's-out', name: 'Output Schema' }]
    vm.onNodeClick({ node: { id: 'node-manual' } })
    await nextTick()
    return wrapper
  }

  it('converts a manual node to an agent through the picker', async () => {
    const wrapper = await mountWithManualNode()
    const vm = wrapper.vm as any

    await wrapper.find('[data-testid="pipeline-editor-convert-to-agent"]').trigger('click')
    await nextTick()
    expect(vm.showAgentPicker).toBe(true)

    // pick agent + connector via the picker state
    vm.pickerAgentId = 'agent-1'
    vm.pickerConnectorId = 'conn-1'
    await nextTick()
    expect(vm.canConvert).toBe(true)

    await vm.convertToAgent()
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls[0]
    expect(post[0]).toBe('/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/convert-to-agent')
    expect((post[1] as any).body.agent_id).toBe('agent-1')
    expect((post[1] as any).body.connector_binding).toEqual({ type: 'slack', instance_id: 'conn-1' })
    expect((post[1] as any).body.model_backend_id).toBe('mb-1')
    expect(vm.showAgentPicker).toBe(false)
    wrapper.unmount()
  })

  it('shows a conversion failure inline', async () => {
    const wrapper = await mountWithManualNode()
    const vm = wrapper.vm as any
    ;(api.POST as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('agent_missing')))
    await wrapper.find('[data-testid="pipeline-editor-convert-to-agent"]').trigger('click')
    await nextTick()
    vm.pickerAgentId = 'agent-1'
    vm.pickerConnectorId = 'conn-1'
    await nextTick()
    await vm.convertToAgent()
    await flushPromises()
    await nextTick()
    // the conversion error renders inside the teleported picker dialog
    expect(document.body.textContent).toContain('agent_missing')
    wrapper.unmount()
  })

  it('reverts an agent node to manual via a snapshot', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'agent',
        agent_id: 'agent-1',
        label: 'Agent Node',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.snapshots = [{ id: 'snap-1', snapshot_version: 2, tag: 'pre-agent' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-revert-to-manual"]').trigger('click')
    await nextTick()
    expect(vm.showRevertDialog).toBe(true)

    vm.revertSnapshotId = 'snap-1'
    await nextTick()
    await vm.revertToManual()
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls[0]
    expect(post[0]).toBe('/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/revert-to-manual')
    expect((post[1] as any).params.query).toEqual({ snapshot_id: 'snap-1' })
    expect(vm.showRevertDialog).toBe(false)
    wrapper.unmount()
  })

  it('shows a revert failure inline', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'Agent Node', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    ;(api.POST as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('revert_denied')))
    await wrapper.find('[data-testid="pipeline-editor-revert-to-manual"]').trigger('click')
    await nextTick()
    vm.revertSnapshotId = 'snap-1'
    await nextTick()
    await vm.revertToManual()
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).toContain('revert_denied')
    wrapper.unmount()
  })

  it('saves a composite from the selected nodes and navigates to the library', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    // with useDataFetch mocked, the graph loaders never run — seed the graph
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'Agent Node', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    await nextTick()
    expect(vm.rawNodes.length).toBeGreaterThan(0)

    // open the Save-as dropdown and pick Composite
    await wrapper.find('[data-testid="pipeline-editor-save-as-template"]').trigger('click')
    await nextTick()
    const compositeItem = wrapper.findAll('button').find((b) => b.text().includes('Composite'))
    await compositeItem!.trigger('click')
    await nextTick()
    expect(vm.showSaveAsComposite).toBe(true)
    // all nodes preselected
    expect(vm.saveAsSelectedNodeIds).toEqual(['node-1'])

    vm.saveAsName = 'My Composite'
    await nextTick()
    await vm.handleSaveAsComposite()
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls[0]
    expect(post[0]).toBe('/api/v1/pipelines/{pipeline_id}/save-as-composite')
    expect((post[1] as any).body).toEqual({
      name: 'My Composite',
      description: null,
      selected_node_ids: ['node-1'],
    })
    expect(vm.showSaveAsComposite).toBe(false)
    wrapper.unmount()
  })

  it('shows a save-as-composite failure inline', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    // seed the graph — openSaveAsComposite preselects from rawNodes
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'Agent Node', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    ;(api.POST as ReturnType<typeof vi.fn>).mockReset()
    ;(api.POST as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {}, error: undefined })
    ;(api.POST as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error('composite_denied')))

    await wrapper.find('[data-testid="pipeline-editor-save-as-template"]').trigger('click')
    await nextTick()
    const compositeItem = wrapper.findAll('button').find((b) => b.text().includes('Composite'))
    await compositeItem!.trigger('click')
    await nextTick()
    vm.saveAsName = 'My Composite'
    await nextTick()
    await vm.handleSaveAsComposite()
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).toContain('composite_denied')
    wrapper.unmount()
  })
})
