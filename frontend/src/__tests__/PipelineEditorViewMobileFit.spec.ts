import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { h, nextTick } from 'vue'

// Viewport-fit coverage for PipelineEditorView: the FAR-1841 mobile fit, the
// FAR-1851 desktop fold fit, and the FAR-1861 mobile-sheet dialog semantics.
// The changed-lines coverage gate reads the unit lcov report, so the Playwright
// specs do not count — these branches must be driven here.

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
import { FOCUSABLE_SELECTOR } from '../composables/useFocusTrap'

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

function sheetFor(wrapper: ReturnType<typeof mount>, closeTestId: string): HTMLElement {
  const close = wrapper.find(`[data-testid="${closeTestId}"]`).element
  return close.closest('aside') as HTMLElement
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
  // Default to the mobile breakpoint so the sheets take their dialog semantics.
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

describe('PipelineEditorView — viewport fit (FAR-1841 mobile / FAR-1851 desktop)', () => {
  it('pins the editor to the leftover viewport height and renders it as an inline style', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(800)
    stubRect(vm.editorRootRef, 180)

    vm.refitEditorFit()
    expect(vm.editorFitHeightPx).toBe(620)
    await nextTick()
    expect(rootStyle(wrapper)).toBe('620px')

    // Re-measuring the same layout must not re-write the reactive height.
    vm.refitEditorFit()
    expect(vm.editorFitHeightPx).toBe(620)

    // FAR-1861: there is no hard 240px floor any more — a sliver of viewport
    // left below the chrome yields to the viewport (30px here) rather than
    // pushing the editor below the fold and re-introducing a page scroll.
    stubRect(vm.editorRootRef, 770)
    vm.refitEditorFit()
    expect(vm.editorFitHeightPx).toBe(30)
    wrapper.unmount()
  })

  it('fits the editor at the desktop breakpoint too (FAR-1851)', async () => {
    setMatchMedia(true)
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(720)
    // Chrome above the editor at desktop is the breadcrumb row (~214px here);
    // the old `h-[calc(100vh-3.5rem)]` class ignored it and overflowed the fold.
    stubRect(vm.editorRootRef, 214)
    vm.refitEditorFit()
    expect(vm.editorFitHeightPx).toBe(506)
    await nextTick()
    expect(rootStyle(wrapper)).toBe('506px')
    wrapper.unmount()
  })

  it('clears the fit when chrome is taller than the viewport', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(500)
    stubRect(vm.editorRootRef, 600)
    vm.refitEditorFit()
    // No space to fit into — the height class fallback applies (page scrolls).
    expect(vm.editorFitHeightPx).toBeNull()
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
    vm.refitEditorFit()
    expect(vm.editorFitHeightPx).toBe(800)

    // Vue nulls the template ref on unmount; the captured call must early-return
    // without taking the "not laid out" clear path, so the pin survives.
    const refit = vm.refitEditorFit.bind(vm)
    wrapper.unmount()
    refit()
    expect(vm.editorFitHeightPx).toBe(800)
  })

  it('clears a previously pinned height when the element is not laid out yet', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.editorFitHeightPx = 321
    stubRect(vm.editorRootRef, 0)

    vm.refitEditorFit()
    expect(vm.editorFitHeightPx).toBeNull()
    wrapper.unmount()
  })

  it('coalesces re-fit requests into one animation frame and cancels a pending frame on unmount', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(1000)
    stubRect(vm.editorRootRef, 200)

    vm.scheduleEditorRefit()
    vm.scheduleEditorRefit()
    expect(rafCallbacks).toHaveLength(1)

    runFrames()
    expect(vm.editorFitHeightPx).toBe(800)

    // A frame still pending when the view unmounts is cancelled.
    vm.scheduleEditorRefit()
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
    expect(() => vm.setupEditorFitObserver(document.createElement('div'))).not.toThrow()
    expect(() => vm.setupEditorFitObserver(null)).not.toThrow()
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

    vm.setupEditorFitObserver(syntheticRoot)

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
})

describe('PipelineEditorView — mobile properties-sheet dialogs (FAR-1861)', () => {
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

  it('closes the edge properties overlay from its header Close button and no longer renders a duplicate bottom Close', async () => {
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
    // The duplicated bottom Close is gone — exactly one Close affordance.
    expect(wrapper.find('[data-testid="pipeline-editor-edge-panel-close"]').exists()).toBe(false)

    await close.trigger('click')
    expect(vm.selectedEdgeData).toBeNull()
    wrapper.unmount()
  })

  it('exposes the mobile sheets as labelled modal dialogs', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    expect(nodeSheet.getAttribute('role')).toBe('dialog')
    expect(nodeSheet.getAttribute('aria-modal')).toBe('true')
    expect(nodeSheet.getAttribute('tabindex')).toBe('-1')
    const nodeTitle = nodeSheet.querySelector('h2') as HTMLElement
    expect(nodeTitle.id).toBe('pipeline-editor-node-sheet-title')
    expect(nodeSheet.getAttribute('aria-labelledby')).toBe('pipeline-editor-node-sheet-title')

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

    const edgeSheet = sheetFor(wrapper, 'pipeline-editor-edge-props-close')
    expect(edgeSheet.getAttribute('role')).toBe('dialog')
    const edgeTitle = edgeSheet.querySelector('h2') as HTMLElement
    expect(edgeTitle.id).toBe('pipeline-editor-edge-sheet-title')
    expect(edgeSheet.getAttribute('aria-labelledby')).toBe('pipeline-editor-edge-sheet-title')
    wrapper.unmount()
  })

  it('does not apply dialog semantics to the docked desktop panels', async () => {
    setMatchMedia(true)
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    expect(nodeSheet.getAttribute('role')).toBeNull()
    expect(nodeSheet.getAttribute('aria-modal')).toBeNull()
    expect(nodeSheet.getAttribute('tabindex')).toBeNull()
    expect(nodeSheet.getAttribute('aria-labelledby')).toBeNull()
    wrapper.unmount()
  })

  it('closes the mobile sheet on Escape', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    nodeSheet.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    expect(vm.selectedNodeData).toBeNull()
    wrapper.unmount()
  })

  it('ignores Escape on the docked desktop panel', async () => {
    setMatchMedia(true)
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    nodeSheet.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    expect(vm.selectedNodeData).not.toBeNull()
    wrapper.unmount()
  })

  it('traps Tab inside the mobile sheet and leaves other keys alone', async () => {
    const wrapper = mountEditor(document.body)
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    const focusable = nodeSheet.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
    const first = focusable[0]
    const middle = focusable[1]
    const last = focusable[focusable.length - 1]
    expect(first).toBeTruthy()
    expect(last).toBeTruthy()
    expect(first).not.toBe(last)

    // Shift+Tab on the first focusable wraps to the last.
    first.focus()
    nodeSheet.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(last)

    // Shift+Tab with focus on the panel container itself also wraps (the
    // freshly-opened sheet focuses the container, so this is the real path).
    nodeSheet.focus()
    nodeSheet.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(last)

    // Shift+Tab from the last focusable is left to the browser (moves backwards).
    last.focus()
    const shiftAtEnd = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })
    nodeSheet.dispatchEvent(shiftAtEnd)
    expect(shiftAtEnd.defaultPrevented).toBe(false)
    expect(document.activeElement).toBe(last)

    // Tab off the last focusable wraps to the first.
    last.focus()
    nodeSheet.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(first)

    // Tab from a middle element is left to the browser.
    middle.focus()
    const tabAtMiddle = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    nodeSheet.dispatchEvent(tabAtMiddle)
    expect(tabAtMiddle.defaultPrevented).toBe(false)
    expect(document.activeElement).toBe(middle)

    // A non-Tab key is not intercepted.
    const other = new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true })
    nodeSheet.dispatchEvent(other)
    expect(other.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('ignores sheet key handling when no sheet is open', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedNodeData = null
    vm.selectedEdgeData = null
    await nextTick()

    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    expect(() => vm.onSheetKeydown(event)).not.toThrow()
    expect(event.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('moves focus into the sheet on open and returns it to the trigger on close', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = 'trigger'
    document.body.appendChild(trigger)
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    const wrapper = mountEditor(document.body)
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await flushPromises()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    expect(document.activeElement).toBe(nodeSheet)

    // A later selection change with focus already INSIDE the sheet must not
    // overwrite the remembered trigger.
    const closeBtn = nodeSheet.querySelector<HTMLElement>('[data-testid="pipeline-editor-node-props-close"]')!
    closeBtn.focus()
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1', label: 'changed' }
    await flushPromises()

    vm.selectedNodeData = null
    await flushPromises()
    expect(document.activeElement).toBe(trigger)

    wrapper.unmount()
    document.body.removeChild(trigger)
  })

  it('pins focus (preventDefault) on Tab in an open sheet with no focusable descendants', async () => {
    const wrapper = mountEditor(document.body)
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    // Strip every focusable descendant, leaving only the focusable container
    // itself — the zero-focusable case. The shared trap preventDefaults so
    // focus cannot escape behind the modal sheet.
    nodeSheet
      .querySelectorAll('a[href], button, input, select, textarea, [tabindex]')
      .forEach((el) => el.remove())
    nodeSheet.focus()
    expect(document.activeElement).toBe(nodeSheet)

    // Called directly (not via dispatchEvent) so an unexpected TypeError in the
    // handler propagates to the assertion rather than being swallowed by jsdom.
    const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })
    expect(() => vm.onSheetKeydown(event)).not.toThrow()
    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(nodeSheet)
    wrapper.unmount()
  })

  it('restores focus to the canvas when the sheet trigger is not focusable', async () => {
    // A VueFlow node/edge wrapper carries no tabindex, so a mobile canvas click
    // leaves `document.activeElement` on <body> — the state a real sheet opens
    // from. Restoring <body> is a no-op, so focus must fall back to the canvas.
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)

    const wrapper = mountEditor(document.body)
    await flushPromises()
    const vm = wrapper.vm as any
    const canvas = wrapper.find('[data-testid="pipeline-editor-canvas"]').element as HTMLElement
    expect(canvas).toBeTruthy()

    vm.selectedEdgeData = null
    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await flushPromises()

    const nodeSheet = sheetFor(wrapper, 'pipeline-editor-node-props-close')
    expect(document.activeElement).toBe(nodeSheet)

    vm.selectedNodeData = null
    await flushPromises()
    expect(document.activeElement).toBe(canvas)

    wrapper.unmount()
  })
})

describe('PipelineEditorView — edge badge variants (FAR-1861 changed-lines gate)', () => {
  // The default mount stub renders only the unnamed slot, so the edge-default
  // template (its loop/llm badge conditionals included) never runs. Rendering
  // it with each variant exercises both arms of the optional-suffix ternaries,
  // which the changed-lines coverage gate scores.
  it('renders the loop and llm edge badges with and without their optional suffix', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.currentTier = 'team'
    store.features = { pipeline_delete: true }
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()

    const wrapper = mount(PipelineEditorView, {
      global: {
        plugins: [pinia, router],
        stubs: {
          VueFlow: {
            render(this: { $slots: Record<string, any> }) {
              const slot = this.$slots['edge-default']
              return h('div', [
                slot?.({ data: { edge_type: 'loop', max_iterations: 5 } }),
                slot?.({ data: { edge_type: 'loop', max_iterations: null } }),
                slot?.({ data: { edge_type: 'llm', routing_label: 'retry' } }),
                slot?.({ data: { edge_type: 'llm', routing_label: null } }),
              ])
            },
          },
          Background: true,
          Controls: true,
        },
      },
    })
    await flushPromises()
    await nextTick()

    const html = wrapper.html()
    expect(html).toContain('(5)')
    // Scope this to the llm badge: a bare `toContain('retry')` is satisfied by
    // the toolbar's `pipeline-editor-retry-policy-toggle` testid even when the
    // `: {routing_label}` suffix ternary renders nothing. `LLM: retry` is a
    // sentinel ONLY the suffix ternary (badge label + ': ' + label) produces.
    expect(html).toContain('LLM: retry')
    wrapper.unmount()
  })
})

describe('PipelineEditorView — toolbar popover anchoring (FAR-1899 changed-lines gate)', () => {
  // The popovers are `position: fixed` and anchored to their trigger's viewport
  // rect so the editor's clipped scroll column (FAR-1861) cannot cut them off.
  // jsdom returns an all-zero rect for every element, so the geometry branches
  // (below vs flipped-above, the min-height clamp, the off-screen right/bottom
  // clamps, and the left-edge clamp) are driven here by stubbing the toggle's
  // rect — the Playwright specs do not feed the unit lcov report the gate reads.
  type Rect = { top: number; right: number; bottom: number; left?: number }

  function fakeRect({ top, right, bottom, left = 0 }: Rect): DOMRect {
    return {
      top,
      right,
      bottom,
      left,
      width: right - left,
      height: bottom - top,
      x: left,
      y: top,
      toJSON: () => ({}),
    } as DOMRect
  }

  function stubToggleRect(wrapper: ReturnType<typeof mount>, testid: string, rect: Rect) {
    const el = wrapper.get(`[data-testid="${testid}"]`).element as HTMLElement
    el.getBoundingClientRect = () => fakeRect(rect)
    return el
  }

  it('returns no inline style when the trigger is not bound yet', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    expect(vm.anchoredPopoverStyle(null, 288)).toEqual({})
    // …and the panel positioner falls back to the class default.
    vm.retryPolicyToggleRef = null
    vm.positionRetryPolicyPanel()
    expect(vm.retryPolicyPanelStyle).toEqual({})
    wrapper.unmount()
  })

  it('anchors the retry panel below the trigger when there is room', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(800)
    // A trigger near the RIGHT edge: the w-72 panel fits right-aligned under it.
    stubToggleRect(wrapper, 'pipeline-editor-retry-policy-toggle', { top: 60, right: 900, bottom: 100 })
    await wrapper.get('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-retry-policy-panel"]').attributes('style') ?? ''
    expect(style).toContain('top: 104px')
    expect(style).toContain('bottom: auto')
    expect(style).toContain('left: auto')
    expect(style).toContain(`right: ${window.innerWidth - 900}px`)
    expect(style).toContain('max-height: 688px')
    wrapper.unmount()
  })

  it('clamps the LEFT edge when the trigger sits near the left edge', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(800)
    // A left-edge trigger: a right-aligned w-72 panel would start at 40 − 288 =
    // −248px, off-screen. The panel must instead left-anchor inside the margin.
    stubToggleRect(wrapper, 'pipeline-editor-retry-policy-toggle', { top: 60, right: 100, bottom: 100, left: 40 })
    await wrapper.get('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-retry-policy-panel"]').attributes('style') ?? ''
    expect(style).toContain('left: 40px')
    expect(style).toContain('right: auto')
    // Still positioned below the trigger.
    expect(style).toContain('top: 104px')
    expect(style).toContain('bottom: auto')
    wrapper.unmount()
  })

  it('flips the panel above the trigger and bounds its height when below is tight', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(300)
    stubToggleRect(wrapper, 'pipeline-editor-autonomy-panel-toggle', { top: 270, right: 200, bottom: 290 })
    await wrapper.get('[data-testid="pipeline-editor-autonomy-panel-toggle"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-autonomy-panel-dialog"]').attributes('style') ?? ''
    expect(style).toContain('top: auto')
    expect(style).toContain('bottom: 34px')
    expect(style).toContain('max-height: 258px')
    wrapper.unmount()
  })

  it('clamps the panel height and keeps it on screen near an edge', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(100)
    stubToggleRect(wrapper, 'pipeline-editor-retry-policy-toggle', { top: 200, right: 2000, bottom: 210 })
    await wrapper.get('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-retry-policy-panel"]').attributes('style') ?? ''
    // Math.max(120, …) keeps a usable minimum even with no real room.
    expect(style).toContain('max-height: 120px')
    // Right/bottom clamp to the margin when the trigger overflows the viewport.
    expect(style).toContain('right: 8px')
    expect(style).toContain('bottom: 8px')
    wrapper.unmount()
  })

  it('keeps the panel below when above is not roomier', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(200)
    stubToggleRect(wrapper, 'pipeline-editor-retry-policy-toggle', { top: 80, right: 200, bottom: 88 })
    await wrapper.get('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-retry-policy-panel"]').attributes('style') ?? ''
    // spaceBelow < 160 but above is smaller, so it stays below (clamped to 120).
    expect(style).toContain('top: 92px')
    expect(style).toContain('bottom: auto')
    expect(style).toContain('max-height: 120px')
    wrapper.unmount()
  })

  it('opens and closes both toolbar popovers through their toggles', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(800)

    const retry = wrapper.get('[data-testid="pipeline-editor-retry-policy-toggle"]')
    await retry.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]').exists()).toBe(true)
    await retry.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]').exists()).toBe(false)

    const autonomy = wrapper.get('[data-testid="pipeline-editor-autonomy-panel-toggle"]')
    await autonomy.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-autonomy-panel-dialog"]').exists()).toBe(true)
    await autonomy.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-autonomy-panel-dialog"]').exists()).toBe(false)

    wrapper.unmount()
  })

  it('left-anchors the save-as-template menu inside the margin (FAR-1899 review note)', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(800)
    // A toolbar trigger near the LEFT edge: the left-anchored w-48 menu hangs
    // off its left edge, clamped inside the margin.
    stubToggleRect(wrapper, 'pipeline-editor-save-as-template', { top: 60, right: 140, bottom: 100, left: 40 })
    await wrapper.get('[data-testid="pipeline-editor-save-as-template"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-save-as-dropdown"]').attributes('style') ?? ''
    expect(style).toContain('left: 40px')
    expect(style).toContain('right: auto')
    expect(style).toContain('top: 104px')
    expect(style).toContain('bottom: auto')
    wrapper.unmount()
  })

  it('clamps the save-as-template menu left edge when the trigger sits near the right edge', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(800)
    // Trigger near the right edge: left = min(rect.left, innerWidth - margin -
    // width) clamps to the viewport's right margin, so the menu stays on screen.
    stubToggleRect(wrapper, 'pipeline-editor-save-as-template', { top: 60, right: 1060, bottom: 100, left: 1000 })
    await wrapper.get('[data-testid="pipeline-editor-save-as-template"]').trigger('click')
    await nextTick()

    const style = wrapper.get('[data-testid="pipeline-editor-save-as-dropdown"]').attributes('style') ?? ''
    expect(style).toContain(`left: ${window.innerWidth - 8 - 192}px`)
    expect(style).toContain('right: auto')
    wrapper.unmount()
  })

  it('drives the left-align branch of anchoredPopoverStyle directly', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const vm = wrapper.vm as any
    setViewport(800)
    const el = document.createElement('button')
    el.getBoundingClientRect = () => fakeRect({ top: 60, right: 140, bottom: 100, left: 40 })
    expect(vm.anchoredPopoverStyle(el, 192, 'left')).toMatchObject({ left: '40px', right: 'auto' })
    wrapper.unmount()
  })

  it('opens and closes the save-as-template menu through its toggle', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    setViewport(800)
    stubToggleRect(wrapper, 'pipeline-editor-save-as-template', { top: 60, right: 140, bottom: 100, left: 40 })

    const trigger = wrapper.get('[data-testid="pipeline-editor-save-as-template"]')
    await trigger.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-save-as-dropdown"]').exists()).toBe(true)

    await trigger.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-save-as-dropdown"]').exists()).toBe(false)

    wrapper.unmount()
  })
})
