import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

// useDataFetch is mocked here so tests can deterministically flip the loading
// branch (the loading ref is exported as __loadingRef); the mount-time loader
// chain with the REAL fetcher is covered in PipelineEditorViewLoad.spec.ts
// (FAR-629 fixed the pageErrorRef TDZ there).
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

  it('closes the run dialog on Escape for keyboard accessibility', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    await nextTick()

    // Seed a node so the Run button is enabled (it is disabled when the
    // canvas has no nodes).
    const vm = wrapper.vm as unknown as { flowNodes: unknown[] }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent' } }]
    await nextTick()

    const runBtn = wrapper.find('[data-testid="pipeline-editor-run"]')
    expect(runBtn.attributes('disabled')).toBeUndefined()

    await runBtn.trigger('click')
    await nextTick()

    const backdrop = wrapper.find('[data-testid="pipeline-editor-run-dialog-backdrop"]')
    expect(backdrop.exists()).toBe(true)

    // Escape on the backdrop closes the dialog (the new keyboard handler).
    // Dispatch a native, bubbling event so the element-level @keydown.escape
    // binding (and the document-level onRunDialogKeydown listener) fire.
    backdrop.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-run-dialog-backdrop"]').exists()).toBe(false)
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

    // FAR-649: the panel defaults to All-errors mode — the granular event
    // checkboxes are hidden until "Choose specific errors" is selected.
    const allRadio = wrapper.find('[data-testid="pipeline-editor-retry-mode-all"]')
    expect(allRadio.exists()).toBe(true)
    expect((allRadio.element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-retry-event-stall"]').exists()).toBe(false)

    await wrapper.find('[data-testid="pipeline-editor-retry-mode-specific"]').setValue('specific')
    await nextTick()

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
    expect((wrapper.vm as any).retryPolicyMode).toBe('specific')
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

    it('blocks save in granular mode with zero events selected (no silent inert policy)', async () => {
      // FAR-649: the FAR-525-era "disable direction" save (`{on: [], ...}`) is
      // gone — granular with zero selected events DISABLES save and warns.
      // The `{on: []}` shape remains valid API-wise, but the editor never
      // produces it.
      const wrapper = await mountWithPolicy({
        on: ['failure', 'stall'],
        max_retries: 2,
        backoff: 7,
        backoff_schedule: { delay_seconds: 90, multiplier: 2 },
      })
      const vm = wrapper.vm as any
      vm.retryPolicyEvents = []
      expect(vm.retryPolicySaveBlocked).toBe(true)
      expect(vm.retryPolicyNoRetriesWarning).toContain('No events selected')

      const callsBefore = vi.mocked(api.PATCH).mock.calls.length
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(vi.mocked(api.PATCH).mock.calls.length).toBe(callsBefore)
      expect(vm.retryPolicyError).toContain('No events selected')
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

    it('rebuilds backoff_schedule from panel state, dropping junk inner keys (All-errors mode, no on key)', async () => {
      const wrapper = await mountWithPolicy({
        on: ['timeout'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 20, multiplier: 3, junk_key: 'hand-edited' },
      })
      const vm = wrapper.vm as any
      vm.retryPolicyMode = 'all'
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        max_retries: 1,
        backoff_schedule: { delay_seconds: 20, multiplier: 3 },
      })
      expect(lastPatchBody().retry_policy.on).toBeUndefined()
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

  describe('FAR-649 retry coverage mode (absent on = all errors)', () => {
    async function mountWithPolicy(retryPolicy: Record<string, unknown> | null) {
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
      return (calls[calls.length - 1][1] as any).body
    }

    it('renders the mode radios with All-errors pre-selected for a no-on policy', async () => {
      const wrapper = await mountWithPolicy({ max_retries: 2 })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyMode).toBe('all')

      await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
      await nextTick()
      const allRadio = wrapper.find('[data-testid="pipeline-editor-retry-mode-all"]')
      expect((allRadio.element as HTMLInputElement).checked).toBe(true)
      expect((wrapper.find('[data-testid="pipeline-editor-retry-mode-specific"]').element as HTMLInputElement).checked).toBe(
        false,
      )
      // granular checkboxes stay hidden until "Choose specific errors"
      expect(wrapper.find('[data-testid="pipeline-editor-retry-event-stall"]').exists()).toBe(false)

      await wrapper.find('[data-testid="pipeline-editor-retry-mode-specific"]').setValue('specific')
      await nextTick()
      expect(wrapper.find('[data-testid="pipeline-editor-retry-event-stall"]').exists()).toBe(true)
    })

    it('saves All-errors mode WITHOUT the on key, preserving schedule and legacy backoff', async () => {
      const wrapper = await mountWithPolicy({
        max_retries: 2,
        backoff: 9,
        backoff_schedule: { delay_seconds: 30, multiplier: 1.5 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyMode).toBe('all')
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        max_retries: 2,
        backoff: 9,
        backoff_schedule: { delay_seconds: 30, multiplier: 1.5 },
      })
      expect(lastPatchBody().retry_policy.on).toBeUndefined()
    })

    it('switching to Choose-specific saves the explicit event list', async () => {
      const wrapper = await mountWithPolicy({ max_retries: 2 })
      const vm = wrapper.vm as any
      vm.retryPolicyMode = 'specific'
      vm.retryPolicyEvents = ['stall', 'timeout']
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: ['stall', 'timeout'],
        max_retries: 2,
        backoff_schedule: { delay_seconds: 45, multiplier: 2 },
      })
    })

    it('loads a stored on: [] as granular with none selected and the no-events warning', async () => {
      const wrapper = await mountWithPolicy({ on: [], max_retries: 2 })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyMode).toBe('specific')
      expect(vm.retryPolicyEvents).toEqual([])
      expect(vm.retryPolicyNoRetriesWarning).toContain('No events selected')
      expect(vm.retryPolicySaveBlocked).toBe(true)

      await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
      await nextTick()
      const warning = wrapper.find('[data-testid="pipeline-editor-retry-policy-warning"]')
      expect(warning.exists()).toBe(true)
      expect(warning.text()).toContain('No events selected')
    })

    it('renders an explicit null on as All-errors (the runtime all-events shape)', async () => {
      const wrapper = await mountWithPolicy({ on: null, max_retries: 2 })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyMode).toBe('all')
      expect(vm.retryPolicyEvents).toEqual([])
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
    // the removed scalar field is never persisted (FAR-820)
    expect(savedNode).not.toHaveProperty('agent_command')
  })

  it('saves a commandless sandbox node as a null list (no scalar invented)', async () => {
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
        // legacy scalar field carried on the node — must be dropped, not persisted
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
    // FAR-820: only the agent_commands list is persisted; a commandless node
    // saves a null list + null joiner, and the dead scalar is never sent.
    expect(savedNode.agent_commands).toBeNull()
    expect(savedNode.commands_concatenation_string).toBeNull()
    expect(savedNode).not.toHaveProperty('agent_command')
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
    // unset joiner saves as the runtime default
    expect(savedNode.commands_concatenation_string).toBe(' && ')
    expect(savedNode.agent_commands).toEqual(['opencode run', '--model oxf'])
    expect(savedNode).not.toHaveProperty('agent_command')
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
    // empty/whitespace rows are dropped; the remaining list wins
    expect(savedNode.agent_commands).toEqual(['cmd-a'])
    expect(savedNode).not.toHaveProperty('agent_command')
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
    // command normalisation still layers on top of the spread; the removed
    // scalar field is never persisted (FAR-820)
    expect(savedNode).not.toHaveProperty('agent_command')
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

  it('shows no command editor or read-only block for an agent node and never fabricates commands', async () => {
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

    // the authoring editor is sandbox-only; an agent node with no agent_commands
    // list shows neither the editor nor a read-only command block
    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-editor"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-readonly"]').exists()).toBe(false)

    await vm.saveGraph()

    const savedNode = (vi.mocked(api.PATCH).mock.calls[0][1] as any).body.nodes[0]
    // the editor never fabricates or rewrites commands on a non-sandbox node
    // (FAR-488a syncs a bound Agent's row); the removed scalar is never sent
    expect(savedNode).not.toHaveProperty('agent_command')
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

  it('closes the run dialog when Escape is fired on the backdrop element (FAR-821 a11y)', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(true)

    const backdrop = wrapper.find('[data-testid="pipeline-editor-run-dialog-backdrop"]')
    expect(backdrop.exists()).toBe(true)
    await backdrop.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('exposes keyboard handlers on the version-timeline toolbar without side effects (FAR-821 a11y)', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    await nextTick()

    const button = wrapper.find('[data-testid="pipeline-editor-version-timeline"]')
    // The toolbar <div> wraps the button with @keydown.enter.stop /
    // @keydown.space.prevent.stop; key events on the button bubble up to it.
    await button.trigger('keydown', { key: 'Enter' })
    await nextTick()
    await button.trigger('keydown', { key: ' ', code: 'Space' })
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-timeline"]').exists()).toBe(true)
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

  it('renders the autonomy ceiling control with the current value and PATCHes on change', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any

    // Seed the loaded pipeline with an explicit ceiling so the control has a
    // non-inherit value to render (the default GET mock omits the field).
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementationOnce((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}') && !url.includes('/graph') && !url.includes('/snapshots')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline', max_autonomy_level: 'notify_on_complete' }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    await vm.loadPipeline()
    await flushPromises()
    await nextTick()

    const select = wrapper.find('[data-testid="pipeline-editor-max-autonomy"]')
    expect(select.exists()).toBe(true)
    expect((select.element as HTMLSelectElement).value).toBe('notify_on_complete')

    // setValue on a SELECT sets the value and dispatches change — the handler runs here.
    await select.setValue('fully_autonomous')
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls.find((c) => 'max_autonomy_level' in ((c[1] as any).body ?? {}))
    expect(patch).toBeTruthy()
    expect((patch as unknown[] | undefined)![1]).toEqual(expect.objectContaining({
      body: { max_autonomy_level: 'fully_autonomous' },
    }))
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
    // Gate-less edges keep their real form state too — the gate-less loop
    // variant is pinned explicitly by the adjacent test.
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

  it('opens a gate-less loop edge with its real type and iterations (the defaults reset no longer wipes them)', async () => {
    // populateEdgeForm resets to defaults FIRST, then applies the edge's own
    // values — previously the gate-less branch reset after assignment, wiping
    // edge_type/max_iterations so a loop edge with no HITL gate opened as
    // "normal" with 0 iterations, and saving the panel would overwrite the
    // graph edge's type (FAR-631).
    const wrapper = await mountWithEdge(edgeFixture({ edge_type: 'loop', max_iterations: 4 }))
    const vm = wrapper.vm as any
    expect(vm.selectedEdgeData.edge_type).toBe('loop')
    // the form carries the edge's actual type and iterations
    expect(vm.edgeForm.edge_type).toBe('loop')
    expect(vm.edgeForm.max_iterations).toBe(4)
    const panel = wrapper.findAll('aside').find((a) => a.text().includes('Edge Properties'))!
    const maxIter = panel.findAll('input[type="number"]').find((i) => Number((i.element as HTMLInputElement).value) === 4)
    expect(maxIter).toBeTruthy()

    // saving keeps the loop type instead of overwriting the graph edge
    // (the save button only renders for gated edges — the gate-less save path
    // is driven at vm level here)
    await vm.saveEdgeConfig()
    await flushPromises()
    await nextTick()
    const patch = vi.mocked(api.PATCH).mock.calls[0]
    expect(patch[0]).toBe('/api/v1/pipelines/{pipeline_id}/graph')
    expect((patch[1] as any).body.edges[0].edge_type).toBe('loop')
    expect((patch[1] as any).body.edges[0].max_iterations).toBe(4)
    wrapper.unmount()
  })

  it('saves the edge config and reloads the graph', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: { label: 'Review gate', description: 'Approve the deploy only after a human reviews the plan.' },
    }))

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

  it('blocks the edge save when the HITL gate has no description (FAR-613)', async () => {
    const wrapper = await mountWithEdge(edgeFixture({ hitl_gate_config: { label: 'Review gate' } }))
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockClear()

    await wrapper.find('[data-testid="pipeline-editor-save-edge"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('HITL gate requires a description')
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(0)
    wrapper.unmount()
  })

  it('blocks the edge save when the HITL gate description is shorter than 20 chars (FAR-613)', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: { label: 'Review gate', description: 'too short' },
    }))
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockClear()

    await wrapper.find('[data-testid="pipeline-editor-save-edge"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('HITL gate requires a description')
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(0)
    wrapper.unmount()
  })

  it('shows an edge save failure inline', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: { label: 'Review gate', description: 'Approve the deploy only after a human reviews the plan.' },
    }))
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

  it('surfaces legacy HITL gates missing descriptions in a dismissible banner (FAR-688)', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: { label: 'Legacy gate', description: 'too short' },
    }))
    const banner = wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('HITL gates are missing descriptions')
    expect(banner.text()).toContain('Gate on edge')

    // Per-kind hint (FAR-688 review fix): an edge violation carries the
    // actionable "open the edge" hint; the node-level API/MCP hint is absent.
    expect(banner.text()).toContain('Open each gate and add one')
    expect(banner.text()).not.toContain('Node-level gates cannot be edited')

    // A FAR-402 HITL node with an empty description is listed as a node issue
    // AND swaps in the node-specific hint (the edge hint stays — the edge
    // violation is still listed).
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'node-9', node_type: 'hitl', hitl_config: {}, label: 'Escalation' }]
    await nextTick()
    const mixedBanner = wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner"]')
    expect(mixedBanner.text()).toContain('HITL node Escalation')
    expect(mixedBanner.text()).toContain('Node-level gates cannot be edited')
    expect(mixedBanner.text()).toContain('Open each gate and add one')

    // Dismissible — the operator can hide it without leaving the editor.
    await wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner-dismiss"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows only the node-level API/MCP hint when just HITL nodes violate the minimum', async () => {
    // The editor has no hitl_config panel for FAR-402 HITL nodes (documented
    // deferral), so the edge hint ("Open each gate and add one") would be
    // unactionable — a node-only violation must NOT render it.
    const wrapper = await mountWithEdge(edgeFixture())
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'node-9', node_type: 'hitl', hitl_config: {}, label: 'Escalation' }]
    await nextTick()
    const banner = wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('HITL node Escalation')
    expect(banner.text()).toContain('Node-level gates cannot be edited')
    expect(banner.text()).not.toContain('Open each gate and add one')
    wrapper.unmount()
  })

  it('never double-lists a node-level gate whose config is injected onto its outgoing edges', async () => {
    const wrapper = await mountWithEdge(edgeFixture({ id: 'edge-hitl', source_node_id: 'node-9', target_node_id: 'node-2' }))
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'node-9', node_type: 'hitl', hitl_config: { label: 'Gate' }, label: 'Escalation' }]
    vm.rawEdges = [
      { id: 'edge-hitl', source_node_id: 'node-9', target_node_id: 'node-2', hitl_gate_config: { label: 'Gate' } },
    ]
    await nextTick()
    const banner = wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('HITL node Escalation')
    expect(banner.text()).not.toContain('Gate on edge')
    wrapper.unmount()
  })

  it('hides the legacy banner when every gate description meets the minimum', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: { label: 'Review gate', description: 'Approve the deploy only after a human reviews the plan.' },
    }))
    expect(wrapper.find('[data-testid="pipeline-editor-legacy-hitl-banner"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('accepts a description padded to 20 code points with emoji (code-point length matches the backend)', async () => {
    // 20 emoji are 40 UTF-16 units — a .length check would reject what the
    // backend's Python len() (code points) accepts. FAR-688 unifies the
    // counting on code points.
    const description = '🚀'.repeat(20)
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_gate_config: { label: 'Review gate', description },
    }))
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockClear()
    await wrapper.find('[data-testid="pipeline-editor-save-edge"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).not.toContain('HITL gate requires a description')
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(1)
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

// ---------------------------------------------------------------------------
// Coverage-boost tests: target the largest uncovered branches in
// PipelineEditorView.vue (SonarCloud ~77% line coverage, 190 uncovered lines).
// ---------------------------------------------------------------------------

describe('PipelineEditorView — coverage: loading / error / edge cases', () => {
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

  it('displays a page error when the graph load fails via pageError ref', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    // Simulate a loadGraph error by setting pageError directly (the mocked
    // useDataFetch never calls the real loaders, so we drive the ref directly).
    vm.pageError = 'graph_unavailable'
    await nextTick()
    expect(wrapper.text()).toContain('graph_unavailable')
    // The toolbar should not render while an error is shown
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clears nodes and edges when loadGraph returns null data', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: null, error: undefined })
      }
      if (url.includes('/api/v1/pipelines/{pipeline_id}')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    expect(vm.rawNodes).toEqual([])
    expect(vm.rawEdges).toEqual([])
    expect(vm.flowNodes).toEqual([])
    expect(vm.flowEdges).toEqual([])
    wrapper.unmount()
  })

  it('displays a page error when loadPipeline fails via pageError ref', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    // Simulate a loadPipeline error by setting pageError directly
    vm.pageError = 'pipeline_not_found'
    await nextTick()
    expect(wrapper.text()).toContain('pipeline_not_found')
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('triggerRun shows save-graph error in the run dialog when saveGraph fails', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'Agent', description: '', position: { x: 0, y: 0 } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="pipeline-editor-run-prompt"]').setValue('go')
    // make saveGraph fail
    ;(api.PATCH as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('save_broken'))
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    // the run dialog should show the save failure, not close
    expect(wrapper.find('[data-testid="pipeline-editor-run-prompt"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('save_broken')
    wrapper.unmount()
  })

  it('triggerRun with empty prompt after confirm sends an empty input_payload', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent', description: '' } }]
    vm.rawNodes = [{ id: 'node-1', node_type: 'agent', label: 'Agent', description: '', position: { x: 0, y: 0 } }]
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-run"]').trigger('click')
    await nextTick()
    // first click: empty prompt warns
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No input provided')
    // second click: proceeds with empty prompt
    await wrapper.find('[data-testid="pipeline-editor-run-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls.find((c) => c[0] === '/api/v1/runs')
    expect(post).toBeTruthy()
    expect((post as unknown[] | undefined)![1]).toEqual(expect.objectContaining({
      body: { pipeline_id: 'test-pipeline-id', input_payload: {} },
    }))
    wrapper.unmount()
  })

  it('addNode appends to both flowNodes and rawNodes with the selected node type', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.rawNodes = []
    vm.flowNodes = []
    await nextTick()

    expect(vm.flowNodes.length).toBe(0)
    // addNode defaults to the newNodeType which is 'agent'
    vm.addNode()
    await nextTick()
    expect(vm.flowNodes.length).toBe(1)
    expect(vm.rawNodes.length).toBe(1)
    expect(vm.rawNodes[0].node_type).toBe('agent')
    expect(vm.flowNodes[0].type).toBe('agent')
    expect(vm.flowNodes[0].data.label).toBeTruthy()
    wrapper.unmount()
  })

  it('syncNodeToFlow propagates label and description changes to the flow node', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'Old Label', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Old Label', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vm.selectedNodeData.label = 'New Label'
    vm.selectedNodeData.description = 'Updated desc'
    vm.syncNodeToFlow()
    await nextTick()

    const fn = vm.flowNodes.find((n: any) => n.id === 'node-1')
    expect(fn.data.label).toBe('New Label')
    expect(fn.data.description).toBe('Updated desc')
    wrapper.unmount()
  })

  it('onPaneClick resets all selections and scope state', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'Agent', description: '', position: { x: 0, y: 0 }, capability_scope: { allowed_tools: ['tool-a'] } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()
    expect(vm.selectedNodeData).toBeTruthy()
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual(['tool-a'])

    vm.onPaneClick()
    await nextTick()
    expect(vm.selectedNodeData).toBeNull()
    expect(vm.selectedEdgeData).toBeNull()
    expect(vm.showSaveAsDropdown).toBe(false)
    expect(vm.nodeCapabilityScope.allowed_connectors).toEqual([])
    expect(vm.nodeCapabilityScope.allowed_tools).toEqual([])
    expect(vm.nodeCapabilityScope.context_scope).toEqual([])
    wrapper.unmount()
  })

  it('convertBackendEdge applies loop style for loop edges and llm style for llm edges', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const loopEdge = vm.convertBackendEdge({ id: 'e1', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'loop', max_iterations: 3 }, 0)
    expect(loopEdge.style.stroke).toBe('#3b82f6')
    expect(loopEdge.style.strokeDasharray).toBe('5,5')
    expect(loopEdge.animated).toBe(true)
    expect(loopEdge.data.max_iterations).toBe(3)

    const llmEdge = vm.convertBackendEdge({ id: 'e2', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'llm', routing_label: 'go' }, 1)
    expect(llmEdge.style.stroke).toBe('#8b5cf6')
    expect(llmEdge.data.routing_label).toBe('go')

    const normalEdge = vm.convertBackendEdge({ id: 'e3', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'normal' }, 2)
    expect(normalEdge.style.stroke).toBe('#888')
    expect(normalEdge.animated).toBe(false)
    wrapper.unmount()
  })

  it('saveEdgeConfig with jmespath condition includes condition in the gate config', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } },
      { id: 'node-2', node_type: 'manual', label: 'B', description: '', position: { x: 100, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'node-1', type: 'agent', data: { label: 'A', description: '' } },
      { id: 'node-2', type: 'manual', data: { label: 'B', description: '' } },
    ]
    vm.rawEdges = [{
      id: 'edge-1', source_node_id: 'node-1', target_node_id: 'node-2',
      edge_type: 'normal', condition_expression: null,
      hitl_gate_config: { label: 'Gate', description: 'Approve the deploy only after a human reviews the plan.' },
    }]
    vm.flowEdges = [{
      id: 'edge-1', source: 'node-1', target: 'node-2',
      data: { hitl_gate_config: { label: 'Gate' }, edge_type: 'normal' },
    }]
    vm.onEdgeClick({ edge: { id: 'edge-1' } })
    await nextTick()

    vm.edgeForm.condition_type = 'jmespath'
    vm.edgeForm.condition = 'status == "approved"'
    await vm.saveEdgeConfig()
    await flushPromises()
    await nextTick()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedEdge = (patch[1] as any).body.edges[0]
    expect(savedEdge.hitl_gate_config.condition).toBe('status == "approved"')
    expect(savedEdge.hitl_gate_config.eval_condition).toBeUndefined()
    wrapper.unmount()
  })

  it('saveEdgeConfig with eval condition type builds eval_condition in gate config', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } },
      { id: 'node-2', node_type: 'manual', label: 'B', description: '', position: { x: 100, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'node-1', type: 'agent', data: { label: 'A', description: '' } },
      { id: 'node-2', type: 'manual', data: { label: 'B', description: '' } },
    ]
    vm.rawEdges = [{
      id: 'edge-1', source_node_id: 'node-1', target_node_id: 'node-2',
      edge_type: 'normal', condition_expression: null,
      hitl_gate_config: { label: 'Gate', description: 'Approve the deploy only after a human reviews the plan.' },
    }]
    vm.flowEdges = [{
      id: 'edge-1', source: 'node-1', target: 'node-2',
      data: { hitl_gate_config: { label: 'Gate' }, edge_type: 'normal' },
    }]
    vm.onEdgeClick({ edge: { id: 'edge-1' } })
    await nextTick()

    vm.edgeForm.condition_type = 'eval'
    vm.edgeForm.eval_name = 'quality_check'
    vm.edgeForm.eval_threshold = 0.9
    vm.edgeForm.eval_operator = 'gte'
    await vm.saveEdgeConfig()
    await flushPromises()
    await nextTick()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedEdge = (patch[1] as any).body.edges[0]
    expect(savedEdge.hitl_gate_config.eval_condition).toEqual({
      eval_name: 'quality_check',
      threshold: 0.9,
      operator: 'gte',
    })
    expect(savedEdge.hitl_gate_config.condition).toBeUndefined()
    wrapper.unmount()
  })

  it('saveEdgeConfig with condition_type none omits condition and eval_condition', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } },
      { id: 'node-2', node_type: 'manual', label: 'B', description: '', position: { x: 100, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'node-1', type: 'agent', data: { label: 'A', description: '' } },
      { id: 'node-2', type: 'manual', data: { label: 'B', description: '' } },
    ]
    vm.rawEdges = [{
      id: 'edge-1', source_node_id: 'node-1', target_node_id: 'node-2',
      edge_type: 'normal', condition_expression: null,
      hitl_gate_config: { label: 'Gate', description: 'Approve the deploy only after a human reviews the plan.' },
    }]
    vm.flowEdges = [{
      id: 'edge-1', source: 'node-1', target: 'node-2',
      data: { hitl_gate_config: { label: 'Gate' }, edge_type: 'normal' },
    }]
    vm.onEdgeClick({ edge: { id: 'edge-1' } })
    await nextTick()

    vm.edgeForm.condition_type = 'none'
    await vm.saveEdgeConfig()
    await flushPromises()
    await nextTick()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedEdge = (patch[1] as any).body.edges[0]
    expect(savedEdge.hitl_gate_config.condition).toBeUndefined()
    expect(savedEdge.hitl_gate_config.eval_condition).toBeUndefined()
    wrapper.unmount()
  })

  it('retryPolicyError surfaces when granular mode has events but zero max retries', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    ;(wrapper.vm as any).pipeline = { retry_policy: { on: ['failure'], max_retries: 2 } }
    ;(wrapper.vm as any).syncRetryPolicyFromPipeline()
    await nextTick()
    const vm = wrapper.vm as any

    vm.retryPolicyMode = 'specific'
    vm.retryPolicyEvents = ['failure']
    vm.retryPolicyMaxRetries = 0
    await vm.saveRetryPolicy()
    await flushPromises()
    await nextTick()

    expect(vm.retryPolicyError).toBeTruthy()
    // With zero max_retries and events selected, the error is about max retries
    expect(vm.retryPolicyError).toContain('Max retries')
    // should NOT have called the API
    expect(vi.mocked(api.PATCH).mock.calls.filter((c) => (c[0] as string).includes('retry_policy') || ((c[1] as any).body?.retry_policy !== undefined)).length).toBe(0)
    wrapper.unmount()
  })

  it('nodeCommandFields returns null fields for sandbox_agent with all-empty commands', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const result = vm.nodeCommandFields({
      node_type: 'sandbox_agent',
      agent_commands: ['  ', ''],
      commands_concatenation_string: ' && ',
    })
    expect(result.agent_commands).toBeNull()
    expect(result.commands_concatenation_string).toBeNull()
    wrapper.unmount()
  })

  it('nodeCommandFields returns empty object for non-sandbox nodes', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const result = vm.nodeCommandFields({
      node_type: 'agent',
      agent_commands: ['cmd-a'],
    })
    expect(result).toEqual({})
    wrapper.unmount()
  })

  it('saveGraph syncs parameter_set_id and overrides into the node payload', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'Agent', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    vm.selectedNodeParamSetId = 'ps-1'
    vm.selectedNodeOverrides = { temperature: 0.5 }
    await vm.saveGraph()
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedNode = (patch[1] as any).body.nodes[0]
    expect(savedNode.parameter_set_id).toBe('ps-1')
    expect(savedNode.parameter_overrides).toEqual({ temperature: 0.5 })
    wrapper.unmount()
  })

  it('saveGraph clears parameter_set_id when no set is selected', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'Agent', description: '', position: { x: 0, y: 0 }, parameter_set_id: 'old-set' },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent', description: '' } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // deselect param set
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

  it('saveGraph includes edges with edge_type, max_iterations, and routing_label', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } },
      { id: 'node-2', node_type: 'manual', label: 'B', description: '', position: { x: 100, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'node-1', type: 'agent', data: { label: 'A', description: '' } },
      { id: 'node-2', type: 'manual', data: { label: 'B', description: '' } },
    ]
    vm.rawEdges = [
      { id: 'e1', source_node_id: 'node-1', target_node_id: 'node-2', edge_type: 'loop', max_iterations: 5, condition_expression: null, hitl_gate_config: null },
      { id: 'e2', source_node_id: 'node-2', target_node_id: 'node-1', edge_type: 'llm', routing_label: 'retry', condition_expression: null, hitl_gate_config: null },
    ]
    await vm.saveGraph()
    await flushPromises()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    const savedEdges = (patch[1] as any).body.edges
    const loopEdge = savedEdges.find((e: any) => e.id === 'e1')
    expect(loopEdge.edge_type).toBe('loop')
    expect(loopEdge.max_iterations).toBe(5)
    const llmEdge = savedEdges.find((e: any) => e.id === 'e2')
    expect(llmEdge.edge_type).toBe('llm')
    expect(llmEdge.routing_label).toBe('retry')
    wrapper.unmount()
  })

  it('retry policy Escape key closes the panel', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()

    await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]').exists()).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('saveAsNewParamSet returns early when no schema is found', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-no-schema', label: 'A', description: '', position: { x: 0, y: 0 } },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'A', description: '' } }]
    vm.agents = [{ id: 'agent-no-schema', name: 'No Schema Agent' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // prompt is mocked to return null (user cancels)
    vi.stubGlobal('prompt', vi.fn().mockReturnValue(null))
    const callsBefore = vi.mocked(api.POST).mock.calls.length
    await vm.saveAsNewParamSet()
    vi.unstubAllGlobals()
    expect(vi.mocked(api.POST).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  it('onParamSetChange clears overrides when deselecting a set', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    vm.selectedNodeParamSetId = 'ps-1'
    vm.selectedNodeOverrides = { temperature: 0.5 }
    vm.onParamSetChange()
    await nextTick()
    // selecting a set populates overrides from the paramSets array
    // but since paramSets is empty, overrides get spread from undefined
    expect(vm.selectedNodeOverrides).toEqual({})

    // now deselect
    vm.selectedNodeParamSetId = undefined
    vm.selectedNodeOverrides = { key: 'val' }
    vm.onParamSetChange()
    await nextTick()
    expect(vm.selectedNodeOverrides).toEqual({})
    wrapper.unmount()
  })

  it('loadLifecycleMaps filters to maps whose stages reference this pipeline', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps/lm-1')) {
        return Promise.resolve({
          id: 'lm-1',
          name: 'Checkout Flow',
          stages: [{ pipeline_id: 'test-pipeline-id' }, { pipeline_id: 'other-pipeline' }],
        })
      }
      if (url.includes('/lifecycle-maps/lm-2')) {
        return Promise.resolve({
          id: 'lm-2',
          name: 'Unrelated Map',
          stages: [{ pipeline_id: 'other-pipeline' }],
        })
      }
      if (url.includes('/lifecycle-maps') && !url.includes('/lm-')) {
        return Promise.resolve([{ id: 'lm-1' }, { id: 'lm-2' }])
      }
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    // The mocked useDataFetch never calls the real loaders — invoke manually
    await vm.loadLifecycleMaps()
    await flushPromises()
    await nextTick()
    expect(vm.linkedLifecycleMaps).toEqual([
      expect.objectContaining({ id: 'lm-1', name: 'Checkout Flow' }),
    ])
    expect(vm.linkedLifecycleMaps.find((m: any) => m.id === 'lm-2')).toBeUndefined()
    wrapper.unmount()
  })

  it('handleDelete navigates to library after successful deletion', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const pushSpy = vi.spyOn(router, 'push')

    await wrapper.find('[data-testid="pipeline-editor-delete"]').trigger('click')
    await nextTick()
    const confirm = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Delete')!
    confirm.click()
    await flushPromises()
    await nextTick()

    expect(vi.mocked(api.DELETE)).toHaveBeenCalled()
    expect(pushSpy).toHaveBeenCalledWith({ name: 'library' })
    wrapper.unmount()
  })

  it('handleRename fails silently when name is empty or whitespace', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-rename"]').trigger('click')
    await nextTick()
    const input = document.querySelector<HTMLInputElement>('#pipelineeditorview-field-1')!
    input.value = '   '
    input.dispatchEvent(new Event('input'))
    await nextTick()
    const callsBefore = vi.mocked(api.PATCH).mock.calls.length
    const confirm = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Save')!
    confirm.click()
    await flushPromises()
    await nextTick()
    // no PATCH call because name is blank
    expect(vi.mocked(api.PATCH).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  it('showSaveAsDropdown toggles open and closed', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'n1', node_type: 'agent', label: 'A', description: '', position: { x: 0, y: 0 } }]
    await nextTick()

    expect(vm.showSaveAsDropdown).toBe(false)
    await wrapper.find('[data-testid="pipeline-editor-save-as-template"]').trigger('click')
    await nextTick()
    expect(vm.showSaveAsDropdown).toBe(true)
    // clicking again closes it
    await wrapper.find('[data-testid="pipeline-editor-save-as-template"]').trigger('click')
    await nextTick()
    expect(vm.showSaveAsDropdown).toBe(false)
    wrapper.unmount()
  })

  it('convertBackendNode handles router and hitl node types', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    const routerNode = vm.convertBackendNode({ id: 'r1', node_type: 'router', label: 'Router', description: 'route', position: { x: 50, y: 50 } })
    expect(routerNode.type).toBe('router')
    expect(routerNode.data.label).toBe('Router')

    const hitlNode = vm.convertBackendNode({ id: 'h1', node_type: 'hitl', label: 'HITL', description: 'gate', position: { x: 100, y: 100 } })
    expect(hitlNode.type).toBe('hitl')
    expect(hitlNode.data.label).toBe('HITL')

    // fallback label when label is empty
    const unlabeled = vm.convertBackendNode({ id: 'u1', node_type: 'agent', label: '', description: '', position: { x: 0, y: 0 } })
    expect(unlabeled.data.label).toContain('u1')
    wrapper.unmount()
  })

  it('displays sandbox_agent node properties: stall_timeout, heartbeat, watch_log, env_vars, context_files, agent_prompt', async () => {
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
        template_id: 'opencode',
        agent_commands: [],
        label: 'Sandbox',
        description: '',
        position: { x: 0, y: 0 },
        stall_timeout_seconds: 120,
        enable_heartbeat: false,
        watch_log_path: '/tmp/output.log',
        stdout_percentage_delta: 15,
        watch_globs: ['*.log', 'output.txt'],
        env_vars: { API_KEY: 'sk-xxx', DEBUG: 'true' },
        context_files: { '/home/user/notes.txt': 'Some notes content', '/home/user/config.json': '{"key":"val"}' },
        agent_prompt: 'You are a helpful assistant that writes code. Please follow the instructions carefully and produce clean, well-structured output.',
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Sandbox', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', model_backend_id: 'mb-1' }]
    vm.modelBackends = [{ id: 'mb-1', display_name: 'Claude', provider: 'anthropic' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // stall timeout displayed
    expect(wrapper.find('[data-testid="pipeline-editor-stall-timeout-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-stall-timeout-value"]').text()).toContain('120')

    // heartbeat disabled
    expect(wrapper.find('[data-testid="pipeline-editor-heartbeat-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-heartbeat-value"]').text()).toContain('Disabled')

    // watch log path
    expect(wrapper.find('[data-testid="pipeline-editor-watch-log-path-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-watch-log-path-value"]').text()).toContain('/tmp/output.log')

    // stdout delta
    expect(wrapper.find('[data-testid="pipeline-editor-stdout-delta-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-stdout-delta-value"]').text()).toContain('15')

    // watch globs
    expect(wrapper.find('[data-testid="pipeline-editor-watch-globs-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-watch-globs-value"]').text()).toContain('*.log')

    // env vars keys displayed
    expect(wrapper.text()).toContain('API_KEY')
    expect(wrapper.text()).toContain('DEBUG')

    // context files displayed
    expect(wrapper.text()).toContain('/home/user/notes.txt')
    expect(wrapper.text()).toContain('bytes')

    // agent prompt (truncated at 300 chars)
    expect(wrapper.text()).toContain('You are a helpful assistant')
    wrapper.unmount()
  })

  it('displays read-only commands for a non-sandbox node that has agent_commands', async () => {
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
        agent_commands: ['echo hello', 'ls -la'],
        commands_concatenation_string: ' && ',
        label: 'Agent Node',
        description: '',
        position: { x: 0, y: 0 },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // Read-only commands block should show
    expect(wrapper.find('[data-testid="pipeline-editor-node-commands-readonly"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('echo hello')
    expect(wrapper.text()).toContain('ls -la')
    expect(wrapper.text()).toContain('&&')
    wrapper.unmount()
  })

  it('loadLifecycleMaps handles a map with no stages gracefully', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps/lm-1')) {
        return Promise.resolve({ id: 'lm-1', name: 'Empty Map', stages: [] })
      }
      if (url.includes('/lifecycle-maps') && !url.includes('/lm-')) {
        return Promise.resolve([{ id: 'lm-1' }])
      }
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    await vm.loadLifecycleMaps()
    await flushPromises()
    // Empty stages means this pipeline is not linked
    expect(vm.linkedLifecycleMaps).toEqual([])
    wrapper.unmount()
  })

  it('loadFolders handles a response wrapped in { items: [...] } shape', async () => {
    useApiFns.get.mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve({ items: [{ id: 'f-1', name: 'Prod', parent_id: null }] })
      return Promise.resolve({ items: [] })
    })
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = await mountEditorLoaded()
    const vm = wrapper.vm as any
    await vm.loadFolders()
    await flushPromises()
    expect(vm.folders).toEqual([{ id: 'f-1', name: 'Prod', parent_id: null }])
    wrapper.unmount()
  })

  it('onParamSetChange selects a set and populates overrides', async () => {
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
    vm.paramSchemas = [{ id: 'ps-1', name: 'Temperature Schema', parameters: [{ name: 'temperature', label: 'Temperature', type: 'number' }] }]
    vm.paramSets = [{ id: 'set-1', parameter_schema_id: 'ps-1', name: 'Creative', values: { temperature: 0.9 } }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()

    // Select a param set
    vm.selectedNodeParamSetId = 'set-1'
    vm.onParamSetChange()
    await nextTick()

    expect(vm.selectedNodeOverrides).toEqual({ temperature: 0.9 })
    expect(vm.selectedNodeData.parameter_set_id).toBe('set-1')
    expect(vm.selectedNodeData.parameter_overrides).toEqual({ temperature: 0.9 })
    wrapper.unmount()
  })

  it('saveAsNewParamSet creates a new param set when prompt returns a name', async () => {
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
    vm.selectedNodeOverrides = { temperature: 0.7 }
    await nextTick()

    // Mock prompt to return a name
    vi.stubGlobal('prompt', vi.fn().mockReturnValue('Creative Preset'))
    await vm.saveAsNewParamSet()
    await flushPromises()
    vi.unstubAllGlobals()

    // POST was called to create the set
    const postCalls = vi.mocked(api.POST).mock.calls
    const createCall = postCalls.find((c) => String(c[0]).includes('/sets'))
    expect(createCall).toBeTruthy()
    expect((createCall as any)[1].body.name).toBe('Creative Preset')
    wrapper.unmount()
  })

  it('onAgentChange resets pickerConnectorId to __all__', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pickerConnectorId = 'some-connector'
    vm.onAgentChange()
    expect(vm.pickerConnectorId).toBe('__all__')
    wrapper.unmount()
  })

  it('handleUnarchive error sets pageError', async () => {
    useApiFns.post.mockRejectedValueOnce(new Error('unarchive_failed'))
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.pipeline = { id: 'test-pipeline-id', name: 'Test', archived_at: '2026-01-01T00:00:00Z' }
    await vm.handleUnarchive()
    await flushPromises()
    expect(vm.pageError).toBeTruthy()
    wrapper.unmount()
  })

  it('saveEdgeConfig success updates selectedEdgeData and repopulates form', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    // Set up a pipeline with nodes and edges
    vm.rawNodes = [
      { id: 'node-1', node_type: 'agent', agent_id: 'agent-1', label: 'A', description: '', position: { x: 0, y: 0 } },
      { id: 'node-2', node_type: 'agent', agent_id: 'agent-1', label: 'B', description: '', position: { x: 200, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'node-1', type: 'agent', data: { label: 'A', description: '' } },
      { id: 'node-2', type: 'agent', data: { label: 'B', description: '' } },
    ]
    vm.rawEdges = [
      { id: 'edge-1', source_node_id: 'node-1', target_node_id: 'node-2', edge_type: 'normal', hitl_gate_config: null, condition_expression: null },
    ]
    vm.flowEdges = [
      { id: 'edge-1', source: 'node-1', target: 'node-2', type: 'smoothstep', data: { edge_type: 'normal', hitl_gate_config: null, condition_expression: null, max_iterations: 0, routing_label: '' } },
    ]
    vm.selectedEdgeData = vm.rawEdges[0]
    vm.edgeForm.edge_type = 'normal'

    // Override the graph GET to return the edge so loadGraph populates rawEdges
    const origGet = vi.mocked(api.GET)
    origGet.mockImplementation((_url: string) => {
      if (String(_url).includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({
          data: {
            nodes: vm.rawNodes,
            edges: [{ id: 'edge-1', source_node_id: 'node-1', target_node_id: 'node-2', edge_type: 'normal', hitl_gate_config: null, condition_expression: null }],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    await vm.saveEdgeConfig()
    await flushPromises()
    expect(vm.savingEdge).toBe(false)
    // After saveEdgeConfig: loadGraph re-fetches, rawEdges is repopulated,
    // and selectedEdgeData is updated with the refreshed edge
    expect(vm.selectedEdgeData).toBeTruthy()
    wrapper.unmount()
  })
})
