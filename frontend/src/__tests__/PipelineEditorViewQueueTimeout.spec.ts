// FAR-1883: per-pipeline capacity-queue timeout override. Structurally the
// sibling of PipelineEditorViewHitlReviewWindow.spec.ts - the control has the
// same three branches (clear -> explicit null; out-of-range/non-integer ->
// reject without calling the API; PATCH failure -> revert to the stored value),
// so it gets the same spec shape covering all three.
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

const TIMEOUT_INPUT = '[data-testid="pipeline-editor-queue-timeout"]'
const SAVE_ERROR = '[data-testid="pipeline-editor-save-error"]'

function defaultPipeline(): Record<string, unknown> {
  return {
    id: 'test-pipeline-id',
    name: 'Test Pipeline',
    queue_timeout_seconds: null,
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

function timeoutPatchBodies(): unknown[] {
  return vi
    .mocked(api.PATCH)
    .mock.calls
    .filter((c) => c[0] === '/api/v1/pipelines/{pipeline_id}')
    .map((c) => (c[1] as { body?: Record<string, unknown> }).body)
    .filter((body) => body !== undefined && 'queue_timeout_seconds' in body)
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = defaultPipeline()
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - per-pipeline queue timeout override', () => {
  it('shows the stored override, or an empty (never) field when unset', async () => {
    state.pipeline.queue_timeout_seconds = 3600
    const set = await mountEditor()
    expect((set.find(TIMEOUT_INPUT).element as HTMLInputElement).value).toBe('3600')
    set.unmount()

    state.pipeline.queue_timeout_seconds = null
    const unset = await mountEditor()
    expect((unset.find(TIMEOUT_INPUT).element as HTMLInputElement).value).toBe('')
    unset.unmount()
  })

  it('saves a valid override via PATCH', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TIMEOUT_INPUT).setValue('3600')
    await flushPromises()

    expect(timeoutPatchBodies()).toEqual([{ queue_timeout_seconds: 3600 }])
    wrapper.unmount()
  })

  it('sends an explicit null to clear the override and queue indefinitely', async () => {
    state.pipeline.queue_timeout_seconds = 3600
    const wrapper = await mountEditor()
    await wrapper.find(TIMEOUT_INPUT).setValue('')
    await flushPromises()

    expect(timeoutPatchBodies()).toEqual([{ queue_timeout_seconds: null }])
    wrapper.unmount()
  })

  it('refuses a value below the 1 second floor without calling the API', async () => {
    state.pipeline.queue_timeout_seconds = 3600
    const wrapper = await mountEditor()
    await wrapper.find(TIMEOUT_INPUT).setValue('0')
    await flushPromises()

    expect(timeoutPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('1 and 604800')
    expect((wrapper.find(TIMEOUT_INPUT).element as HTMLInputElement).value).toBe('3600')
    wrapper.unmount()
  })

  it('refuses a value above the 604800 second ceiling without calling the API', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TIMEOUT_INPUT).setValue('604801')
    await flushPromises()

    expect(timeoutPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('1 and 604800')
    expect((wrapper.find(TIMEOUT_INPUT).element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('refuses a non-integer second count without calling the API', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TIMEOUT_INPUT).setValue('600.5')
    await flushPromises()

    expect(timeoutPatchBodies()).toHaveLength(0)
    expect(wrapper.find(SAVE_ERROR).text()).toContain('1 and 604800')
    wrapper.unmount()
  })

  it('surfaces a PATCH failure and reverts the field to the stored value', async () => {
    state.pipeline.queue_timeout_seconds = 3600
    vi.mocked(api.PATCH).mockImplementationOnce(() => Promise.reject(new Error('queue_timeout_rejected')))
    const wrapper = await mountEditor()
    await wrapper.find(TIMEOUT_INPUT).setValue('900')
    await flushPromises()

    expect(wrapper.find(SAVE_ERROR).text()).toContain('queue_timeout_rejected')
    expect((wrapper.find(TIMEOUT_INPUT).element as HTMLInputElement).value).toBe('3600')
    wrapper.unmount()
  })

  it('clears a nullish input and still saves when no pipeline is loaded', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any

    // A nullish input resolves through the ''-fallback to an explicit clear.
    vm.queueTimeoutInput = null
    await vm.updateQueueTimeout()
    await flushPromises()
    expect(timeoutPatchBodies()).toContainEqual({ queue_timeout_seconds: null })

    // With no pipeline object loaded the stored-value write is skipped, not
    // crashed; the PATCH still lands.
    vm.pipeline = null
    vm.queueTimeoutInput = '3600'
    await vm.updateQueueTimeout()
    await flushPromises()
    expect(timeoutPatchBodies()).toContainEqual({ queue_timeout_seconds: 3600 })
    wrapper.unmount()
  })
})
