import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

// Spies for the VueFlow store surface the editor consumes. The editor must
// call fitView automatically once the pane is ready and nodes exist (Fix:
// fit the view before first render), latch its guard only on a SUCCESSFUL
// fit, and keep re-attempting on pane-ready / node changes while fitView
// keeps resolving false — these mocks prove it.
const { fitViewSpy, paneReadyHandlers } = vi.hoisted(() => ({
  fitViewSpy: vi.fn(),
  paneReadyHandlers: [] as Array<() => void>,
}))

vi.mock('@vue-flow/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@vue-flow/core')>()
  return {
    ...actual,
    useVueFlow: () => ({
      fitView: fitViewSpy,
      onPaneReady: (cb: () => void) => {
        paneReadyHandlers.push(cb)
      },
    }),
  }
})

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
  })),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
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

import PipelineEditorView from '../views/PipelineEditorView.vue'

const seededFlowNodes = [{ id: 'node-1', type: 'agent', position: { x: 0, y: 0 }, data: { label: 'Agent Node', description: '' } }]

function mountEditor() {
  return mount(PipelineEditorView, {
    global: {
      plugins: [createPinia()],
      stubs: {
        VueFlow: { template: '<div><slot /></div>' },
        Background: true,
        Controls: true,
      },
    },
  })
}

// Attribute selectors resolve to the component whose ROOT element carries the
// data-testid; the shared WrapperLike type omits props(), so re-type it here.
function componentByTestid(wrapper: VueWrapper, testid: string): { props: (name: string) => unknown } {
  return wrapper.findComponent(`[data-testid="${testid}"]`) as unknown as { props: (name: string) => unknown }
}

describe('PipelineEditorView toolbar & fit-on-load', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // clearAllMocks keeps implementations, so reset the spy fully and restore
    // the real contract: fitView resolves true on a successful fit.
    fitViewSpy.mockReset()
    fitViewSpy.mockResolvedValue(true)
    paneReadyHandlers.length = 0
  })

  it('fits the view automatically once nodes exist after mount', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    // No nodes yet — the initial fit must not fire on an empty graph.
    expect(fitViewSpy).not.toHaveBeenCalled()

    // Simulate the async graph load delivering nodes.
    ;(wrapper.vm as any).flowNodes = seededFlowNodes
    await nextTick()

    // runInitialFitView awaits nextTick + animation frames before fitting.
    await vi.waitFor(() => expect(fitViewSpy).toHaveBeenCalledTimes(1))
  })

  it('fits the view when the pane becomes ready with nodes already present', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    ;(wrapper.vm as any).flowNodes = seededFlowNodes
    await nextTick()
    expect(paneReadyHandlers.length).toBeGreaterThan(0)

    // Simulate VueFlow emitting its paneReady lifecycle hook.
    paneReadyHandlers[paneReadyHandlers.length - 1]()
    await vi.waitFor(() => expect(fitViewSpy).toHaveBeenCalled())
  })

  it('re-attempts the fit on pane-ready when the first attempt resolves false', async () => {
    // First (nodes-watch) attempt fails per VueFlow's contract: fitView
    // resolves false when the viewport has nothing measurable to fit.
    fitViewSpy.mockResolvedValueOnce(false)
    const wrapper = mountEditor()
    await flushPromises()

    ;(wrapper.vm as any).flowNodes = seededFlowNodes
    await vi.waitFor(() => expect(fitViewSpy).toHaveBeenCalledTimes(1))

    // A failed attempt must NOT latch the guard — pane-ready re-attempts.
    paneReadyHandlers[paneReadyHandlers.length - 1]()
    await vi.waitFor(() => expect(fitViewSpy).toHaveBeenCalledTimes(2))

    // The retry succeeds and latches: further triggers stay no-ops.
    paneReadyHandlers[paneReadyHandlers.length - 1]()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(fitViewSpy).toHaveBeenCalledTimes(2)
  })

  it('does not re-attempt the fit once a successful fit has latched', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    ;(wrapper.vm as any).flowNodes = seededFlowNodes
    await vi.waitFor(() => expect(fitViewSpy).toHaveBeenCalledTimes(1))
    // waitFor observes the call; give the resolved true a tick to latch.
    await nextTick()

    // Later pane-ready emissions and node-count changes must be no-ops now.
    paneReadyHandlers[paneReadyHandlers.length - 1]()
    ;(wrapper.vm as any).flowNodes = [
      ...seededFlowNodes,
      { id: 'node-2', type: 'agent', position: { x: 10, y: 10 }, data: { label: 'Second', description: '' } },
    ]
    // A duplicate attempt would fire after nextTick + two animation frames.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(fitViewSpy).toHaveBeenCalledTimes(1)
  })

  it('renders the docked toolbar with grouped controls and the Fit to View label', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const toolbar = wrapper.find('[data-testid="pipeline-editor-toolbar"]')
    expect(toolbar.exists()).toBe(true)
    // The docked bar must be in-flow (no absolute overlay classes) so it never
    // covers the canvas or node details.
    expect(toolbar.classes()).not.toContain('absolute')

    for (const group of ['identity', 'file', 'run', 'settings', 'canvas']) {
      expect(
        wrapper.find(`[data-testid="pipeline-editor-toolbar-group-${group}"]`).exists(),
        `toolbar group ${group}`,
      ).toBe(true)
    }

    const fitButton = wrapper.find('[data-testid="pipeline-editor-fit-view"]')
    expect(fitButton.exists()).toBe(true)
    expect(fitButton.text()).toContain('Fit to View')
  })

  it('sizes the toolbar node-type Select to the uniform h-7 row height', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    // PrimeVue's Select keeps its default ~2.5rem height inside the h-7
    // toolbar row; the toolbar-select class (scoped style) is what sizes it
    // back to 1.75rem. Both the canvas-tools Select and the empty-state
    // Select (which share the visual row) must carry it — dialog/aside
    // Selects must not.
    const toolbarSelect = wrapper.find('[data-testid="pipeline-editor-node-type-select"]')
    expect(toolbarSelect.exists()).toBe(true)
    expect(toolbarSelect.classes()).toContain('toolbar-select')

    // flowNodes is empty at mount, so the empty-state overlay renders its own
    // node-type picker for the same row.
    expect(wrapper.findAll('.toolbar-select').length).toBeGreaterThanOrEqual(2)
  })

  it('anchors the node-type dropdown overlay to its trigger', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const select = componentByTestid(wrapper, 'pipeline-editor-node-type-select')
    // appendTo="self" renders the options overlay inside the select's own
    // position:relative wrapper, so items can never detach to the viewport
    // origin (the body-appended mispositioning reported on this page).
    expect(select.props('appendTo')).toBe('self')
    // PrimeVue v5 consumes aria-label as the ariaLabel prop.
    expect(select.props('ariaLabel')).toBe('New node type')
  })

  it('labels and anchors the convert-to-agent dropdown', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    ;(wrapper.vm as any).showAgentPicker = true
    await nextTick()
    await nextTick()

    // PrimeVue Dialog teleports to document.body — search the whole document.
    const el = document.querySelector('[data-testid="pipeline-editor-agent-select"]')
    expect(el).not.toBeNull()

    const select = componentByTestid(wrapper, 'pipeline-editor-agent-select')
    expect(select.props('appendTo')).toBe('self')
    // PrimeVue v5 consumes aria-label as the ariaLabel prop.
    expect(select.props('ariaLabel')).toBe('Agent to convert to')
  })
})
