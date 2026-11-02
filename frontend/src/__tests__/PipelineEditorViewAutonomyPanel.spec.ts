// FAR-1696 (ADR 043 S5): the progressive-autonomy visibility panel popover on
// the pipeline editor toolbar — open/close/Escape/focus wiring plus the
// toggle→panel aria-controls relationship.
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

const TOGGLE = '[data-testid="pipeline-editor-autonomy-panel-toggle"]'
const DIALOG = '[data-testid="pipeline-editor-autonomy-panel-dialog"]'
const CLOSE = '[data-testid="pipeline-editor-autonomy-panel-close"]'
const PANEL = '[data-testid="progressive-autonomy-panel"]'
const PIPELINE_PATH = '/api/v1/pipelines/{pipeline_id}'

function defaultPipeline(): Record<string, unknown> {
  return {
    id: 'test-pipeline-id',
    name: 'Test Pipeline',
    default_autonomy_level: 'manual_approval',
    max_autonomy_level: 'fully_autonomous',
    earned_autonomy_level: 'notify_on_complete',
    earned_autonomy_updated_at: '2026-09-01T10:00:00Z',
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

function pipelineGetCalls(): unknown[] {
  return vi
    .mocked(api.GET)
    .mock.calls.filter((c) => c[0] === PIPELINE_PATH)
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = defaultPipeline()
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - progressive autonomy panel popover', () => {
  it('starts closed and opens from the toolbar toggle with the aria-controls wiring', async () => {
    const wrapper = await mountEditor()
    expect(wrapper.find(DIALOG).exists()).toBe(false)
    expect(wrapper.find(TOGGLE).attributes('aria-expanded')).toBe('false')

    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()

    expect(wrapper.find(DIALOG).exists()).toBe(true)
    expect(wrapper.find(PANEL).exists()).toBe(true)
    expect(wrapper.find(TOGGLE).attributes('aria-expanded')).toBe('true')
    // The toggle points AT the dialog id, so assistive tech can announce it.
    expect(wrapper.find(TOGGLE).attributes('aria-controls')).toBe(
      'pipeline-editor-autonomy-panel-dialog',
    )
    expect(wrapper.find(DIALOG).attributes('id')).toBe('pipeline-editor-autonomy-panel-dialog')
    wrapper.unmount()
  })

  it('projects the pipeline levels into the panel', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="progressive-autonomy-default"]').text()).toContain(
      'Manual approval',
    )
    expect(wrapper.find('[data-testid="progressive-autonomy-ceiling"]').text()).toContain(
      'Fully autonomous',
    )
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Notify on complete',
    )
    wrapper.unmount()
  })

  it('toggles closed on a second click, dropping the dialog from the DOM', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()
    expect(wrapper.find(DIALOG).exists()).toBe(true)

    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()
    expect(wrapper.find(DIALOG).exists()).toBe(false)
    expect(wrapper.find(TOGGLE).attributes('aria-expanded')).toBe('false')
    wrapper.unmount()
  })

  it('closes via the panel close button', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()

    await wrapper.find(CLOSE).trigger('click')
    await nextTick()
    expect(wrapper.find(DIALOG).exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes on Escape while open and ignores unrelated keys', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }))
    await nextTick()
    expect(wrapper.find(DIALOG).exists()).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(wrapper.find(DIALOG).exists()).toBe(false)
    wrapper.unmount()
  })

  it('traps Shift+Tab inside the panel so focus cannot escape behind it', async () => {
    const wrapper = await mountEditor()
    // Attach the rendered tree to the document: `HTMLElement.focus()` on a
    // detached node is a no-op in jsdom, so the focus move the trap performs
    // is only observable when the tree is in the document.
    document.body.append(wrapper.element)
    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()
    const dialog = wrapper.find(DIALOG).element as HTMLElement

    const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, cancelable: true })
    document.dispatchEvent(event)
    await nextTick()

    expect(event.defaultPrevented).toBe(true)
    expect(dialog.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).not.toBe(dialog)
    wrapper.unmount()
  })

  it('ignores an Escape key while the panel is already closed', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as unknown as {
      onAutonomyPanelKeydown: (e: { key: string; preventDefault: () => void }) => void
    }
    const event = { key: 'Escape', preventDefault: vi.fn() }

    vm.onAutonomyPanelKeydown(event)

    expect(event.preventDefault).not.toHaveBeenCalled()
    expect(wrapper.find(DIALOG).exists()).toBe(false)
    wrapper.unmount()
  })

  it('refetches the pipeline when the panel reports a change', async () => {
    const wrapper = await mountEditor()
    const before = pipelineGetCalls().length

    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()
    const vm = wrapper.vm as unknown as { onAutonomyChanged: () => Promise<void> }

    await vm.onAutonomyChanged()
    await flushPromises()

    expect(pipelineGetCalls().length).toBeGreaterThan(before)
    wrapper.unmount()
  })

  it('still renders the popover before a pipeline object is loaded', async () => {
    const wrapper = await mountEditor()
    await wrapper.find(TOGGLE).trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as { pipeline: unknown }
    vm.pipeline = null
    await nextTick()

    // The optional-chained level reads tolerate a null pipeline.
    expect(wrapper.find(DIALOG).exists()).toBe(true)
    wrapper.unmount()
  })
})
