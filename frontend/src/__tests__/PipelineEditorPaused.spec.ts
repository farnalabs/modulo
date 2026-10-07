// FAR-1530: per-pipeline Paused execution state (editor half) — Pause/Resume
// toolbar buttons and the persistent "no runs will start" banner.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, computed } from 'vue'

const state = vi.hoisted(() => ({
  pipeline: {} as Record<string, unknown>,
  orgRole: 'admin' as string | null,
}))

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ items: [] }),
    post: (url: string) => postMock(url).then(() => ({ ...state.pipeline })),
  }),
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

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { usePlanStore } from '../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
    { path: '/library', name: 'library', component: { template: '<div />' } },
  ],
})

const PAUSE = '[data-testid="pipeline-editor-pause"]'
const RESUME = '[data-testid="pipeline-editor-resume"]'
const BANNER = '[data-testid="pipeline-editor-paused-banner"]'

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

// The view captures `route.params.id` at setup, so seed it before every mount
// so pipeline-scoped calls (POST .../pause, .../resume) carry the real id.
beforeEach(async () => {
  vi.clearAllMocks()
  postMock.mockResolvedValue(undefined)
  state.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline', archived_at: null, run_enabled: true, run_disabled_reason: null }
  state.orgRole = 'admin'
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - per-pipeline Paused state', () => {
  it('offers Pause (not Resume) while the pipeline runs, and posts /pause', async () => {
    const wrapper = await mountEditor()

    expect(wrapper.find(BANNER).exists()).toBe(false)
    const pause = wrapper.find(PAUSE)
    expect(pause.exists()).toBe(true)
    expect(wrapper.find(RESUME).exists()).toBe(false)

    await pause.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/pipelines/test-pipeline-id/pause')
    wrapper.unmount()
  })

  it('shows the persistent banner and flips to Resume when the pipeline is paused', async () => {
    state.pipeline = { ...state.pipeline, run_enabled: false, run_disabled_reason: 'operator' }
    const wrapper = await mountEditor()

    expect(wrapper.find(PAUSE).exists()).toBe(false)
    const resume = wrapper.find(RESUME)
    expect(resume.exists()).toBe(true)
    expect((resume.element as HTMLButtonElement).disabled).toBe(false)

    const banner = wrapper.find(BANNER)
    expect(banner.exists()).toBe(true)
    // Announced without stealing focus: aria-live only. This view's FAR-1257
    // comment codifies aria-live WITHOUT role="status" (SonarCloud Web:S6819),
    // so the banner must not carry a role either.
    expect(banner.attributes('role')).toBeUndefined()
    expect(banner.attributes('aria-live')).toBe('polite')
    expect(banner.text()).toContain('paused')
    // The pause blocks EVERY origin, so the copy must not read as if only
    // triggers + manual runs were affected.
    expect(banner.text()).toContain('any source')
    // Operator pauses carry no circuit-breaker remediation line.
    expect(banner.text()).not.toContain('circuit breaker')

    await resume.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/pipelines/test-pipeline-id/resume')
    wrapper.unmount()
  })

  it('blocks Resume and names the tripped breaker while run_disabled_reason is circuit_breaker', async () => {
    state.pipeline = { ...state.pipeline, run_enabled: false, run_disabled_reason: 'circuit_breaker' }
    const wrapper = await mountEditor()

    const resume = wrapper.find(RESUME)
    expect(resume.exists()).toBe(true)
    expect((resume.element as HTMLButtonElement).disabled).toBe(true)
    expect(resume.attributes('title')).toContain('circuit breaker')

    const banner = wrapper.find(BANNER)
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('circuit breaker')
    expect(banner.text()).toContain('org admin')

    // The blocked button must not reach the API.
    await resume.trigger('click')
    await flushPromises()
    expect(postMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers neither Pause nor Resume while the pipeline is archived', async () => {
    state.pipeline = { ...state.pipeline, archived_at: '2026-01-01T00:00:00Z' }
    const wrapper = await mountEditor()

    // An archived pipeline is inert: it offers Unarchive, never an
    // execution-state toggle (same !archived_at guard as Archive/Unarchive).
    expect(wrapper.find(PAUSE).exists()).toBe(false)
    expect(wrapper.find(RESUME).exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-unarchive"]').exists()).toBe(true)
    wrapper.unmount()
  })

  // FAR-1530: pause/resume are in-place toggles, so a failed toggle must
  // surface through the inline toolbar error (saveGraphError) rather than
  // replacing the whole editor via pageError — the open graph must survive.
  it('reports a failed pause through the inline toolbar error', async () => {
    const wrapper = await mountEditor()

    postMock.mockRejectedValueOnce(new Error('Pause failed'))
    await wrapper.find(PAUSE).trigger('click')
    await flushPromises()

    const errorEl = wrapper.find('[data-testid="pipeline-editor-save-error"]')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toContain('Failed to pause pipeline')
    expect(errorEl.text()).toContain('Pause failed')
    wrapper.unmount()
  })

  it('reports a failed resume through the inline toolbar error', async () => {
    state.pipeline = { ...state.pipeline, run_enabled: false, run_disabled_reason: 'operator' }
    const wrapper = await mountEditor()

    postMock.mockRejectedValueOnce(new Error('Resume failed'))
    await wrapper.find(RESUME).trigger('click')
    await flushPromises()

    const errorEl = wrapper.find('[data-testid="pipeline-editor-save-error"]')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toContain('Failed to resume pipeline')
    expect(errorEl.text()).toContain('Resume failed')
    wrapper.unmount()
  })
})
