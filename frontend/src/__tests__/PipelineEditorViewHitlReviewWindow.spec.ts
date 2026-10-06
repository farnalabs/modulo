// FAR-1257: per-pipeline HITL review window override + the ownerless-gate
// advisory in the HITL review config panel.
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

import { api } from '../lib/api/client'
import PipelineEditorView from '../views/PipelineEditorView.vue'
import { usePlanStore } from '../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
    { path: '/library', name: 'library', component: { template: '<div />' } },
  ],
})

const WINDOW_INPUT = '[data-testid="pipeline-editor-hitl-review-window"]'
const SAVE_ERROR = '[data-testid="pipeline-editor-save-error"]'
const ADVISORY = '[data-testid="pipeline-editor-ownerless-gate-advisory"]'
const SAVE_EDGE = '[data-testid="pipeline-editor-save-edge"]'

function defaultPipeline(): Record<string, unknown> {
  return {
    id: 'test-pipeline-id',
    name: 'Test Pipeline',
    hitl_review_window_seconds: null,
    owner_team_id: null,
    business_owner_id: null,
    reliability_owner_id: null,
  }
}

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

function windowPatchBodies(): unknown[] {
  return vi
    .mocked(api.PATCH)
    .mock.calls
    .filter((c) => c[0] === '/api/v1/pipelines/{pipeline_id}')
    .map((c) => (c[1] as { body?: Record<string, unknown> }).body)
    .filter((body) => body !== undefined && 'hitl_review_window_seconds' in body)
}

function graphPatchBodies(): unknown[] {
  return vi
    .mocked(api.PATCH)
    .mock.calls
    .filter((c) => c[0] === '/api/v1/pipelines/{pipeline_id}/graph')
    .map((c) => (c[1] as { body?: Record<string, unknown> }).body)
}

function edgeFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'edge-1',
    source_node_id: 'node-1',
    target_node_id: 'node-2',
    edge_type: 'normal',
    condition_expression: null,
    hitl_review_config: {
      label: 'Review gate',
      description: 'Approve the deploy only after a human reviews the plan.',
      claim_expiry_minutes: 30,
      human_only: true,
    },
    ...overrides,
  }
}

async function mountWithEdge(edge: Record<string, unknown>) {
  const wrapper = await mountEditor()
  const vm = wrapper.vm as any
  vm.rawEdges = [edge]
  vm.flowEdges = [{
    id: edge.id,
    source: edge.source_node_id,
    target: edge.target_node_id,
    data: { hitl_review_config: edge.hitl_review_config, edge_type: edge.edge_type },
  }]
  vm.onEdgeClick({ edge: { id: edge.id } })
  await nextTick()
  return wrapper
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = defaultPipeline()
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - per-pipeline HITL review window override', () => {
  it('shows the stored override, or an empty (inherit) field when unset', async () => {
    state.pipeline.hitl_review_window_seconds = 600
    const set = await mountEditor()
    expect((set.find(WINDOW_INPUT).element as HTMLInputElement).value).toBe('600')
    set.unmount()

    state.pipeline.hitl_review_window_seconds = null
    const unset = await mountEditor()
    expect((unset.find(WINDOW_INPUT).element as HTMLInputElement).value).toBe('')
    unset.unmount()
  })

  it('saves a valid override via PATCH', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(WINDOW_INPUT).setValue('600')
    await flushPromises()

    expect(windowPatchBodies()).toEqual([{ hitl_review_window_seconds: 600 }])
    wrapper.unmount()
  })

  it('sends an explicit null to clear the override and inherit the org default', async () => {
    state.pipeline.hitl_review_window_seconds = 600
    const wrapper = await mountEditor()
    await wrapper.find(WINDOW_INPUT).setValue('')
    await flushPromises()

    expect(windowPatchBodies()).toEqual([{ hitl_review_window_seconds: null }])
    wrapper.unmount()
  })

  it('refuses a value below the 60 second floor without calling the API', async () => {
    state.pipeline.hitl_review_window_seconds = 600
    const wrapper = await mountEditor()
    await wrapper.find(WINDOW_INPUT).setValue('59')
    await flushPromises()

    expect(windowPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('60 and 604800')
    expect((wrapper.find(WINDOW_INPUT).element as HTMLInputElement).value).toBe('600')
    wrapper.unmount()
  })

  it('refuses a value above the 604800 second ceiling without calling the API', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(WINDOW_INPUT).setValue('604801')
    await flushPromises()

    expect(windowPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('60 and 604800')
    expect((wrapper.find(WINDOW_INPUT).element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('refuses a non-integer second count without calling the API', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(WINDOW_INPUT).setValue('600.5')
    await flushPromises()

    expect(windowPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('60 and 604800')
    wrapper.unmount()
  })

  it('surfaces a PATCH failure and reverts the field to the stored value', async () => {
    state.pipeline.hitl_review_window_seconds = 600
    vi.mocked(api.PATCH).mockImplementationOnce(() => Promise.reject(new Error('window_rejected')))
    const wrapper = await mountEditor()
    await wrapper.find(WINDOW_INPUT).setValue('900')
    await flushPromises()

    expect(wrapper.find(SAVE_ERROR).text()).toContain('window_rejected')
    expect((wrapper.find(WINDOW_INPUT).element as HTMLInputElement).value).toBe('600')
    wrapper.unmount()
  })
})

describe('PipelineEditorView - ownerless-gate advisory', () => {
  it('advises when a human-only gate has no required team and the pipeline has no owner', async () => {
    const wrapper = await mountWithEdge(edgeFixture())
    expect(wrapper.find(ADVISORY).exists()).toBe(true)
    expect(wrapper.find(ADVISORY).attributes('aria-live')).toBe('polite')
    wrapper.unmount()
  })

  it('stays silent when the pipeline has an owner team', async () => {
    state.pipeline.owner_team_id = 'team-1'
    const wrapper = await mountWithEdge(edgeFixture())
    expect(wrapper.find(ADVISORY).exists()).toBe(false)
    wrapper.unmount()
  })

  it('stays silent when the pipeline has a business owner', async () => {
    state.pipeline.business_owner_id = 'user-1'
    const wrapper = await mountWithEdge(edgeFixture())
    expect(wrapper.find(ADVISORY).exists()).toBe(false)
    wrapper.unmount()
  })

  it('stays silent when the pipeline has only a reliability owner', async () => {
    state.pipeline.reliability_owner_id = 'user-2'
    const wrapper = await mountWithEdge(edgeFixture())
    expect(wrapper.find(ADVISORY).exists()).toBe(false)
    wrapper.unmount()
  })

  it('stays silent when the gate declares a required team', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_review_config: {
        label: 'Review gate',
        description: 'Approve the deploy only after a human reviews the plan.',
        claim_expiry_minutes: 30,
        human_only: true,
        required_team_id: 'team-9',
      },
    }))
    expect(wrapper.find(ADVISORY).exists()).toBe(false)
    wrapper.unmount()
  })

  it('stays silent when the gate is not human-only', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_review_config: {
        label: 'Review gate',
        description: 'Approve the deploy only after a human reviews the plan.',
        claim_expiry_minutes: 30,
        human_only: false,
      },
    }))
    expect(wrapper.find(ADVISORY).exists()).toBe(false)
    wrapper.unmount()
  })

  it('is advisory only: the gate still saves while the advisory is showing', async () => {
    const wrapper = await mountWithEdge(edgeFixture())
    expect(wrapper.find(ADVISORY).exists()).toBe(true)

    await wrapper.find(SAVE_EDGE).trigger('click')
    await flushPromises()

    expect(graphPatchBodies()).toHaveLength(1)
    wrapper.unmount()
  })

  it('carries over on_reject and correction_target the form cannot edit', async () => {
    const wrapper = await mountWithEdge(edgeFixture({
      hitl_review_config: {
        label: 'Review gate',
        description: 'Approve the deploy only after a human reviews the plan.',
        claim_expiry_minutes: 30,
        human_only: true,
        on_reject: 'proceed',
        correction_target: 'node-9',
      },
    }))

    await wrapper.find(SAVE_EDGE).trigger('click')
    await flushPromises()

    const bodies = graphPatchBodies()
    expect(bodies).toHaveLength(1)
    const config = (bodies[0] as { edges: Array<{ hitl_review_config: Record<string, unknown> }> }).edges[0]
      .hitl_review_config
    expect(config.on_reject).toBe('proceed')
    expect(config.correction_target).toBe('node-9')
    wrapper.unmount()
  })

  it('does not advise before an edge is selected', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    expect(vm.ownerlessGateAdvisory).toBe(false)
    wrapper.unmount()
  })

  it('clears a nullish input and still saves when no pipeline is loaded', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any

    // A nullish input resolves through the ''-fallback to an explicit clear.
    vm.hitlReviewWindowInput = null
    await vm.updateHitlReviewWindow()
    await flushPromises()
    expect(windowPatchBodies()).toContainEqual({ hitl_review_window_seconds: null })

    // With no pipeline object loaded the stored-value write is skipped, not
    // crashed; the PATCH still lands.
    vm.pipeline = null
    vm.hitlReviewWindowInput = '600'
    await vm.updateHitlReviewWindow()
    await flushPromises()
    expect(windowPatchBodies()).toContainEqual({ hitl_review_window_seconds: 600 })
    wrapper.unmount()
  })
})
