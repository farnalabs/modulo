import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    }),
    post: vi.fn().mockResolvedValue({}),
  })),
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

import PipelineEditorView from '../views/PipelineEditorView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
  ],
})

function mountEditor() {
  return mount(PipelineEditorView, {
    global: {
      plugins: [createPinia(), router],
      stubs: {
        VueFlow: { template: '<div><slot /></div>' },
        Background: true,
        Controls: true,
      },
    },
  })
}





describe('PipelineEditorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
})
