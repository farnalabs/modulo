import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

// FAR-1841: unit coverage for the mobile-viewport fit logic added to
// PipelineEditorView (refitMobileEditor / mobileFitStyle / the MutationObserver
// wiring and the two new header Close handlers). The changed-lines coverage
// gate reads the unit lcov report, so the Playwright specs added alongside the
// feature do not count — the branches must be driven here.

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

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

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

vi.mock('../lib/api/client', () => {
  const get = (url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    }
    if (url.includes('/api/v1/pipelines/{pipeline_id}')) {
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

const originalMatchMedia = window.matchMedia
const originalInnerHeight = Object.getOwnPropertyDescriptor(window, 'innerHeight')

function setMatchMedia(matches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

function setViewport(innerHeight: number) {
  Object.defineProperty(window, 'innerHeight', { value: innerHeight, configurable: true, writable: true })
  Object.defineProperty(window, 'scrollY', { value: 0, configurable: true, writable: true })
}

function stubRect(el: HTMLElement, top: number) {
  el.getBoundingClientRect = () =>
    ({
      top,
      left: 0,
      right: 0,
      bottom: top,
      width: 0,
      height: 0,
      x: 0,
      y: top,
      toJSON: () => ({}),
    }) as DOMRect
}

// Capture requestAnimationFrame callbacks so a scheduled re-fit can be driven
// deterministically instead of waiting on the browser frame clock.
let rafCallbacks: FrameRequestCallback[] = []
function runFrames() {
  const callbacks = rafCallbacks.splice(0)
  for (const cb of callbacks) cb(0)
}

function flushMutations() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function rootStyle(wrapper: ReturnType<typeof mount>): string {
  return (wrapper.find('[data-testid="pipeline-editor-root"]').element as HTMLElement).style.height
}

function mountEditor(attachTo?: HTMLElement) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = { pipeline_delete: true, pipeline_diff_rollback: true }
  return mount(PipelineEditorView, {
    attachTo,
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

beforeEach(async () => {
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  useApiFns.get.mockReset()
  useApiFns.post.mockReset()
  useApiFns.get.mockImplementation((url: string) => {
    if (url.includes('/lifecycle-maps')) return Promise.resolve([])
    if (url.includes('/pipeline-folders')) return Promise.resolve([])
    return Promise.resolve({ items: [] })
  })
  useApiFns.post.mockResolvedValue({})
  // Default to the mobile breakpoint so onMounted takes the fitting path.
  setMatchMedia(false)
  rafCallbacks = []
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
    rafCallbacks.push(cb)
    return rafCallbacks.length
  })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
  window.matchMedia = originalMatchMedia
  if (originalInnerHeight) Object.defineProperty(window, 'innerHeight', originalInnerHeight)
})

describe('PipelineEditorView — mobile viewport fit (FAR-1841)', () => {
  it('pins the editor to the leftover viewport height on mobile and renders it as an inline style', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(800)
    stubRect(vm.editorRootRef, 180)

    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBe(620)
    await nextTick()
    expect(rootStyle(wrapper)).toBe('620px')

    // Re-measuring the same layout must not re-write the reactive height.
    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBe(620)

    // A sliver of viewport left below the chrome clamps to the minimum height.
    stubRect(vm.editorRootRef, 770)
    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBe(240)
    wrapper.unmount()
  })

  it('clears the pinned height at the desktop breakpoint', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(800)
    stubRect(vm.editorRootRef, 200)
    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBe(600)
    await nextTick()
    expect(rootStyle(wrapper)).toBe('600px')

    // At >=768px the inline height is cleared so the height class applies.
    setMatchMedia(true)
    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBeNull()
    await nextTick()
    expect(rootStyle(wrapper)).toBe('')
    wrapper.unmount()
  })

  it('does nothing when the editor root ref is not bound', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(900)
    stubRect(vm.editorRootRef, 100)
    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBe(800)

    // Vue nulls the template ref on unmount; the captured call must early-return
    // without taking the "not laid out" clear path, so the pin survives.
    const refit = vm.refitMobileEditor.bind(vm)
    wrapper.unmount()
    refit()
    expect(vm.mobileFitHeightPx).toBe(800)
  })

  it('clears a previously pinned height when the element is not laid out yet', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.mobileFitHeightPx = 321
    stubRect(vm.editorRootRef, 0)

    vm.refitMobileEditor()
    expect(vm.mobileFitHeightPx).toBeNull()
    wrapper.unmount()
  })

  it('coalesces re-fit requests into one animation frame and cancels a pending frame on unmount', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(1000)
    stubRect(vm.editorRootRef, 200)

    vm.scheduleMobileRefit()
    vm.scheduleMobileRefit()
    expect(rafCallbacks).toHaveLength(1)

    runFrames()
    expect(vm.mobileFitHeightPx).toBe(800)

    // A frame still pending when the view unmounts is cancelled.
    vm.scheduleMobileRefit()
    expect(rafCallbacks).toHaveLength(1)
    wrapper.unmount()
    expect(window.cancelAnimationFrame).toHaveBeenCalledWith(1)
  })

  it('ignores a detached root and a null root when wiring the chrome observer', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // Detached element (no parentElement) and an absent root are the two
    // defensive guards around the observer wiring.
    expect(() => vm.setupMobileFitObserver(document.createElement('div'))).not.toThrow()
    expect(() => vm.setupMobileFitObserver(null)).not.toThrow()
    wrapper.unmount()
  })

  it('observes chrome above the editor and re-fits when it mutates', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any

    // A synthetic editor root whose parent also holds a chrome block above it.
    const parent = document.createElement('div')
    const banner = document.createElement('div')
    banner.className = 'banner'
    parent.appendChild(banner)
    const syntheticRoot = document.createElement('div')
    parent.appendChild(syntheticRoot)
    document.body.appendChild(parent)

    vm.setupMobileFitObserver(syntheticRoot)

    // Attribute mutation on a chrome sibling (not a childList mutation).
    banner.className = 'banner changed'
    await flushMutations()
    expect(rafCallbacks).toHaveLength(1)
    runFrames()

    // childList mutation inside a chrome sibling (target is not the parent).
    banner.appendChild(document.createElement('i'))
    await flushMutations()
    expect(rafCallbacks).toHaveLength(1)
    runFrames()

    // childList mutation on the parent itself → re-observe + re-fit.
    parent.appendChild(document.createElement('span'))
    await flushMutations()
    expect(rafCallbacks).toHaveLength(1)
    runFrames()

    document.body.removeChild(parent)
    wrapper.unmount()
  })

  it('closes the node properties overlay from its header Close button', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const close = wrapper.find('[data-testid="pipeline-editor-node-props-close"]')
    expect(close.exists()).toBe(true)
    await close.trigger('click')
    expect(vm.selectedNodeData).toBeNull()
    wrapper.unmount()
  })

  it('closes the edge properties overlay from its header Close button', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedNodeData = null
    vm.selectedEdgeData = {
      id: 'e1',
      source_node_id: 'n1',
      target_node_id: 'n2',
      edge_type: 'normal',
      hitl_review_config: null,
      condition_expression: null,
    }
    await nextTick()

    const close = wrapper.find('[data-testid="pipeline-editor-edge-props-close"]')
    expect(close.exists()).toBe(true)
    await close.trigger('click')
    expect(vm.selectedEdgeData).toBeNull()
    wrapper.unmount()
  })
})
