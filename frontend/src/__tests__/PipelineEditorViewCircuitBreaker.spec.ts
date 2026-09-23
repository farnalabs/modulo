// FAR-1182: pipeline monthly spend circuit breaker - threshold field + tripped badge/reset.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, computed } from 'vue'

const state = vi.hoisted(() => ({
  pipeline: {} as Record<string, unknown>,
  orgRole: 'admin' as string | null,
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ items: [] }), post: vi.fn().mockResolvedValue({}) }),
}))

vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({
    jwtPayload: computed(() => null),
    userId: computed(() => 'user-1'),
    orgId: computed(() => 'org-1'),
    orgRole: computed(() => state.orgRole),
    isSystemAdmin: computed(() => false),
    isOperator: computed(() => state.orgRole === 'admin' || state.orgRole === 'operator'),
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

const THRESHOLD = '[data-testid="pipeline-editor-circuit-breaker-threshold"]'
const TRIPPED = '[data-testid="pipeline-editor-circuit-breaker-tripped"]'
const RESET = '[data-testid="pipeline-editor-circuit-breaker-reset"]'
const SAVE_ERROR = '[data-testid="pipeline-editor-save-error"]'

async function mountEditor() {
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  // Community plan: the breaker must work with every plan feature off.
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

function thresholdPatchBodies(): unknown[] {
  return vi
    .mocked(api.PATCH)
    .mock.calls.map((c) => (c[1] as { body?: Record<string, unknown> }).body)
    .filter((body) => body !== undefined && 'circuit_breaker_threshold' in body)
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline', circuit_breaker_threshold: null, circuit_breaker_tripped: false }
  state.orgRole = 'admin'
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - spend circuit breaker', () => {
  it('shows the configured threshold, or an empty (disabled) field', async () => {
    state.pipeline.circuit_breaker_threshold = 42.5
    const wrapper = await mountEditor()
    expect((wrapper.find(THRESHOLD).element as HTMLInputElement).value).toBe('42.5')
    wrapper.unmount()

    state.pipeline.circuit_breaker_threshold = null
    const empty = await mountEditor()
    expect((empty.find(THRESHOLD).element as HTMLInputElement).value).toBe('')
    empty.unmount()
  })

  it('saves a positive threshold via PATCH', async () => {
    vi.mocked(api.PATCH).mockResolvedValueOnce({ data: { circuit_breaker_threshold: 100 }, error: undefined } as never)
    const wrapper = await mountEditor()

    await wrapper.find(THRESHOLD).setValue('100')
    await flushPromises()

    expect(thresholdPatchBodies()).toEqual([{ circuit_breaker_threshold: 100 }])
    wrapper.unmount()
  })

  it('clearing the field disables the breaker (sends null)', async () => {
    state.pipeline.circuit_breaker_threshold = 10
    const wrapper = await mountEditor()

    await wrapper.find(THRESHOLD).setValue('')
    await flushPromises()

    expect(thresholdPatchBodies()).toEqual([{ circuit_breaker_threshold: null }])
    wrapper.unmount()
  })

  it('rejects a non-positive threshold without calling the API', async () => {
    const wrapper = await mountEditor()

    await wrapper.find(THRESHOLD).setValue('0')
    await flushPromises()

    expect(thresholdPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('greater than 0')
    wrapper.unmount()
  })

  it('surfaces an API failure when saving the threshold', async () => {
    vi.mocked(api.PATCH).mockImplementationOnce(() => Promise.reject(new Error('threshold_rejected')))
    const wrapper = await mountEditor()

    await wrapper.find(THRESHOLD).setValue('5')
    await flushPromises()

    expect(wrapper.find(SAVE_ERROR).text()).toContain('threshold_rejected')
    wrapper.unmount()
  })

  it('hides the tripped badge while the breaker is not tripped', async () => {
    const wrapper = await mountEditor()
    expect(wrapper.find(TRIPPED).exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the tripped badge and lets an org admin reset it', async () => {
    state.pipeline.circuit_breaker_tripped = true
    const wrapper = await mountEditor()

    expect(wrapper.find(TRIPPED).text()).toContain('Circuit breaker tripped')
    await wrapper.find(RESET).trigger('click')
    await flushPromises()

    const resetCall = vi
      .mocked(api.POST)
      .mock.calls.find((c) => c[0] === '/api/v1/admin/costs/circuit-breaker/{pipeline_id}/reset')
    expect(resetCall?.[1]).toEqual(expect.objectContaining({ params: { path: { pipeline_id: 'test-pipeline-id' } } }))
    expect(wrapper.find(TRIPPED).exists()).toBe(false)
    wrapper.unmount()
  })

  it('does not offer the reset action to non-admins', async () => {
    state.pipeline.circuit_breaker_tripped = true
    state.orgRole = 'operator'
    const wrapper = await mountEditor()

    expect(wrapper.find(TRIPPED).exists()).toBe(true)
    expect(wrapper.find(RESET).exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the badge and shows an error when the reset fails', async () => {
    state.pipeline.circuit_breaker_tripped = true
    vi.mocked(api.POST).mockImplementationOnce(() => Promise.reject(new Error('reset_denied')))
    const wrapper = await mountEditor()

    await wrapper.find(RESET).trigger('click')
    await flushPromises()

    expect(wrapper.find(TRIPPED).exists()).toBe(true)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('reset_denied')
    wrapper.unmount()
  })
})
