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
    const supportedEvents = ['stall', 'timeout', 'failure']
    for (const event of supportedEvents) {
      const checkbox = wrapper.find(`[data-testid="pipeline-editor-retry-event-${event}"]`)
      expect(checkbox.exists(), `retry event checkbox for ${event}`).toBe(true)
    }

    // eval_failed is NOT a selectable option: the backend retry_policy allowlist
    // (api/routes/pipelines.py) rejects it and the engine never auto-resurrects
    // guardrail-blocked runs (core/pipeline_engine/recovery.py), so offering it
    // would be a no-op trap where every save fails with 422.
    const evalCheckbox = wrapper.find('[data-testid="pipeline-editor-retry-event-eval_failed"]')
    expect(evalCheckbox.exists()).toBe(false)

    // round-trip: a policy persisted with an unsupported event (e.g. eval_failed)
    // or a bogus value is loaded safely — unknown events are dropped, supported
    // ones are kept, and the editor never crashes on stale payloads.
    ;(wrapper.vm as any).pipeline = {
      retry_policy: { on: ['eval_failed', 'stall', 'bogus_event'], max_retries: 2 },
    }
    ;(wrapper.vm as any).syncRetryPolicyFromPipeline()
    await nextTick()
    expect((wrapper.vm as any).retryPolicyEvents).toEqual(['stall'])
    const stallCheckbox = wrapper.find('[data-testid="pipeline-editor-retry-event-stall"]')
    expect((stallCheckbox.element as HTMLInputElement).checked).toBe(true)
  })
})
