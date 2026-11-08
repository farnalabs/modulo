// FAR-2463: the pipeline editor surfaces governance state it already holds —
// the autonomy ceiling, the configured concurrency cap and the spend
// circuit-breaker threshold in the toolbar, plus HITL/gate pills on canvas
// nodes. Live run counts and current spend are not loaded by this view, so
// they must never be rendered.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, computed } from 'vue'

const state = vi.hoisted(() => ({
  pipeline: {} as Record<string, unknown>,
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ items: [] }), post: vi.fn().mockResolvedValue({}) }),
}))

vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({
    jwtPayload: computed(() => null),
    userId: computed(() => 'user-1'),
    orgId: computed(() => 'org-1'),
    orgRole: computed(() => 'admin'),
    isSystemAdmin: computed(() => false),
    isOperator: computed(() => true),
    permissions: computed(() => null),
  }),
}))

vi.mock('../lib/api/client', () => {
  const get = (url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { ...state.pipeline }, error: undefined })
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
    { path: '/library', name: 'library', component: { template: '<div />' } },
  ],
})

const AUTONOMY = '[data-testid="pipeline-editor-governance-autonomy"]'
const CAPACITY = '[data-testid="pipeline-editor-governance-capacity"]'
const BREAKER = '[data-testid="pipeline-editor-governance-breaker"]'
const GROUP = '[data-testid="pipeline-editor-governance-visibility"]'

async function mountEditor() {
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'community'
  store.features = {}
  const wrapper = mount(PipelineEditorView, {
    global: {
      plugins: [pinia, router],
      stubs: { VueFlow: { template: '<div><slot /></div>' }, Background: true, Controls: true },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline' }
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - governance visibility', () => {
  it('shows the autonomy ceiling, capacity cap and breaker threshold it already holds', async () => {
    state.pipeline = {
      id: 'test-pipeline-id',
      name: 'Test Pipeline',
      max_autonomy_level: 'notify_on_complete',
      max_concurrent_runs: 3,
      circuit_breaker_threshold: 100,
    }
    const wrapper = await mountEditor()

    expect(wrapper.find(GROUP).attributes('aria-label')).toBe('Governance visibility')
    expect(wrapper.find(AUTONOMY).text()).toBe('Ceiling: Notify on complete')
    expect(wrapper.find(CAPACITY).text()).toContain('3 concurrent runs')
    expect(wrapper.find(BREAKER).text()).toContain('$100.00')

    // The view loads no live run count and no current spend, so the ticket's
    // illustrative "3/5 runs • $47.20/$100mo" must never be fabricated.
    expect(wrapper.text()).not.toContain('$47.20')
    expect(wrapper.text()).not.toContain('/5 runs')
    wrapper.unmount()
  })

  it('reads the pipeline default as the effective ceiling when no ceiling is set', async () => {
    state.pipeline = {
      id: 'test-pipeline-id',
      name: 'Test Pipeline',
      default_autonomy_level: 'manual_approval',
      max_autonomy_level: null,
      max_concurrent_runs: 5,
      circuit_breaker_threshold: null,
    }
    const wrapper = await mountEditor()

    expect(wrapper.find(AUTONOMY).text()).toBe('Ceiling: Manual approval')
    expect(wrapper.find(AUTONOMY).attributes('title')).toContain('inherits its autonomy ceiling')
    expect(wrapper.find(BREAKER).exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders no governance chips at all when the pipeline record carries no values', async () => {
    const wrapper = await mountEditor()

    expect(wrapper.find(GROUP).exists()).toBe(true)
    expect(wrapper.find(AUTONOMY).exists()).toBe(false)
    expect(wrapper.find(CAPACITY).exists()).toBe(false)
    expect(wrapper.find(BREAKER).exists()).toBe(false)
    wrapper.unmount()
  })

  it('threads the node gate contract onto the canvas node data', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as unknown as {
      convertBackendNode: (node: Record<string, unknown>) => { data: Record<string, unknown> }
    }

    const converted = vm.convertBackendNode({
      id: 'gate-node',
      node_type: 'hitl',
      label: 'Escalation',
      hitl_config: { human_only: true, claim_expiry_minutes: 240, required_team_id: 'team-1' },
      policy_gate: { action: 'block', eval_id: 'eval-1' },
    })

    expect(converted.data.hitl_config).toEqual({ human_only: true, claim_expiry_minutes: 240, required_team_id: 'team-1' })
    expect(converted.data.policy_gate).toEqual({ action: 'block', eval_id: 'eval-1' })
    wrapper.unmount()
  })
})
