import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

// FAR-2464: canvas-scoped keyboard operation. These cover the command palette
// (Cmd/Ctrl+K, search, run), node navigation and nudging, keyboard edge
// creation and the polite live region that announces each mutation. All
// shortcuts are scoped to the canvas element, so typing in a real input must
// never open the palette.

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

const originalMatchMedia = window.matchMedia

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

async function mountEditor() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = { pipeline_delete: true, pipeline_diff_rollback: true }
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
  const wrapper = mount(PipelineEditorView, {
    attachTo: document.body,
    global: {
      plugins: [pinia],
      stubs: {
        VueFlow: { template: '<div><slot /></div>' },
        Background: true,
        Controls: true,
      },
    },
  })
  await flushPromises()
  return wrapper
}

function canvasOf(wrapper: ReturnType<typeof mount>): HTMLElement {
  return wrapper.find('[data-testid="pipeline-editor-canvas"]').element as HTMLElement
}

function keydown(target: Element, key: string, init: KeyboardEventInit = {}) {
  return target.dispatchEvent(
    new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }),
  )
}

function announcement(wrapper: ReturnType<typeof mount>): string {
  return wrapper.find('[data-testid="pipeline-editor-canvas-announcement"]').text()
}

function palette(wrapper: ReturnType<typeof mount>) {
  return wrapper.find('[data-testid="pipeline-editor-command-palette"]')
}

const mounted: ReturnType<typeof mount>[] = []

beforeEach(() => {
  setMatchMedia(true)
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
})

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount()
  document.body.innerHTML = ''
  window.matchMedia = originalMatchMedia
  vi.restoreAllMocks()
})

async function mountedEditor() {
  const wrapper = await mountEditor()
  mounted.push(wrapper)
  return wrapper
}

describe('PipelineEditorView canvas keyboard (FAR-2464)', () => {
  it('opens the canvas command palette on Cmd/Ctrl+K from the canvas', async () => {
    const wrapper = await mountedEditor()
    expect(palette(wrapper).exists()).toBe(false)

    keydown(canvasOf(wrapper), 'k', { ctrlKey: true })
    await nextTick()

    expect(palette(wrapper).exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-command-palette-input"]').exists()).toBe(true)
  })

  it('never opens the palette while the user is typing in an input', async () => {
    const wrapper = await mountedEditor()
    const input = document.createElement('input')
    canvasOf(wrapper).appendChild(input)

    keydown(input, 'k', { ctrlKey: true })
    await nextTick()

    expect(palette(wrapper).exists()).toBe(false)
  })

  it('closes the palette on Escape and returns focus to the canvas', async () => {
    const wrapper = await mountedEditor()
    const canvas = canvasOf(wrapper)
    keydown(canvas, 'k', { metaKey: true })
    await nextTick()

    const search = wrapper.find('[data-testid="pipeline-editor-command-palette-input"]').element as HTMLInputElement
    keydown(search, 'Escape')
    await nextTick()

    expect(palette(wrapper).exists()).toBe(false)
    expect(document.activeElement).toBe(canvas)
  })

  it('filters commands and runs the selected one with Enter', async () => {
    const wrapper = await mountedEditor()
    const canvas = canvasOf(wrapper)
    keydown(canvas, 'k', { ctrlKey: true })
    await nextTick()

    const search = wrapper.find('[data-testid="pipeline-editor-command-palette-input"]').element as HTMLInputElement
    search.value = 'fit'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    const options = wrapper.findAll('[data-testid="pipeline-editor-command-palette-list"] [role="option"]')
    expect(options).toHaveLength(1)
    expect(options[0].text()).toContain('Fit view')

    keydown(search, 'Enter')
    await nextTick()

    expect(palette(wrapper).exists()).toBe(false)
    expect(announcement(wrapper)).toContain('Canvas fitted to view')
  })

  it('resets the selection index when a query filters the list, so Enter runs the highlighted command', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    keydown(canvasOf(wrapper), 'k', { ctrlKey: true })
    await nextTick()

    const search = wrapper.find('[data-testid="pipeline-editor-command-palette-input"]').element as HTMLInputElement

    // Move the selection down before typing, so a stale index would point past
    // the shrunken result set (or at a different command).
    keydown(search, 'ArrowDown')
    keydown(search, 'ArrowDown')
    keydown(search, 'ArrowDown')
    expect(vm.canvasPaletteIndex).toBe(3)

    search.value = 'fit'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(vm.canvasPaletteIndex).toBe(0)
    const options = wrapper.findAll('[data-testid="pipeline-editor-command-palette-list"] [role="option"]')
    expect(options).toHaveLength(1)
    expect(options[0].text()).toContain('Fit view')

    keydown(search, 'Enter')
    await nextTick()

    expect(palette(wrapper).exists()).toBe(false)
    expect(announcement(wrapper)).toContain('Canvas fitted to view')
  })

  it('reports when no command matches the query', async () => {
    const wrapper = await mountedEditor()
    keydown(canvasOf(wrapper), 'k', { ctrlKey: true })
    await nextTick()

    const search = wrapper.find('[data-testid="pipeline-editor-command-palette-input"]').element as HTMLInputElement
    search.value = 'no-such-command'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(wrapper.findAll('[data-testid="pipeline-editor-command-palette-list"] [role="option"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('No matching canvas commands')
  })

  it('adds an agent node from the palette and announces it', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any

    vm.runCanvasCommand('add-node')
    await nextTick()

    expect(vm.flowNodes).toHaveLength(1)
    expect(announcement(wrapper)).toContain('Agent node added')
  })

  it('moves focus between nodes with the arrow keys', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
    ]
    await nextTick()

    const canvas = canvasOf(wrapper)
    const left = document.createElement('div')
    left.className = 'vue-flow__node'
    left.setAttribute('data-id', 'a')
    left.tabIndex = 0
    const right = document.createElement('div')
    right.className = 'vue-flow__node'
    right.setAttribute('data-id', 'b')
    right.tabIndex = 0
    canvas.append(left, right)
    left.focus()

    keydown(canvas, 'ArrowRight')
    await nextTick()

    expect(document.activeElement).toBe(right)
  })

  it('nudges the focused node with Shift+Arrow and announces the move', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.selectedNodeData = { id: 'a', node_type: 'agent' }
    vm.rawNodes = [{ id: 'a', node_type: 'agent', position: { x: 0, y: 0 } }]
    vm.flowNodes = [{ id: 'a', position: { x: 0, y: 0 } }]
    await nextTick()

    keydown(canvasOf(wrapper), 'ArrowDown', { shiftKey: true })
    await nextTick()

    expect(vm.flowNodes[0].position).toEqual({ x: 0, y: 20 })
    expect(announcement(wrapper)).toContain('Node moved')
  })

  it('starts a keyboard connection from the selected node and cancels with Escape', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.selectedNodeData = { id: 'a', node_type: 'agent' }
    await nextTick()

    keydown(canvasOf(wrapper), 'e')
    await nextTick()

    expect(vm.keyboardConnectSourceId).toBe('a')
    expect(announcement(wrapper)).toContain('Connection started')

    keydown(canvasOf(wrapper), 'Escape')
    await nextTick()

    expect(vm.keyboardConnectSourceId).toBeNull()
    expect(announcement(wrapper)).toContain('Connection cancelled')
  })

  it('explains that a connection needs a selected node', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.selectedNodeData = null
    vm.flowNodes = []

    keydown(canvasOf(wrapper), 'e')
    await nextTick()

    expect(vm.keyboardConnectSourceId).toBeNull()
    expect(announcement(wrapper)).toContain('Select a node')
  })

  it('creates an edge between two nodes and refuses a duplicate', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'a', node_type: 'agent', position: { x: 0, y: 0 } },
      { id: 'b', node_type: 'agent', position: { x: 200, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
    ]
    vm.rawEdges = []
    vm.flowEdges = []

    vm.connectNodes('a', 'b')
    await nextTick()

    expect(vm.rawEdges).toHaveLength(1)
    expect(vm.rawEdges[0].source_node_id).toBe('a')
    expect(vm.rawEdges[0].target_node_id).toBe('b')
    expect(announcement(wrapper)).toContain('Connection created')

    vm.connectNodes('a', 'b')
    await nextTick()

    expect(vm.rawEdges).toHaveLength(1)
    expect(announcement(wrapper)).toContain('already connected')
  })

  it('opens the edge inspector when an edge is selected and reports a missing edge', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any

    vm.selectedEdgeData = null
    vm.toggleSelectedEdgeHitl()
    await nextTick()
    expect(announcement(wrapper)).toContain('Select an edge')

    vm.selectedEdgeData = { id: 'e1' }
    vm.toggleSelectedEdgeHitl()
    await nextTick()
    expect(announcement(wrapper)).toContain('HITL gate enabled')
  })
})

// Second pass: the remaining branches of the FAR-2464 canvas-keyboard code
// (direct handlers, navigation geometry, read-only historic view and the
// mobile-sheet focus return). The changed-lines coverage gate measures branch
// coverage too, and none of these arms are reachable from the user-level
// tests above.
describe('PipelineEditorView canvas keyboard — branch coverage (FAR-2464)', () => {
  it('runs a palette command from a pointer click on its option', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    keydown(canvasOf(wrapper), 'k', { ctrlKey: true })
    await nextTick()

    await wrapper.findAll('[data-testid="pipeline-editor-command-palette-list"] [role="option"]')[0].trigger('click')
    await nextTick()

    expect(palette(wrapper).exists()).toBe(false)
    expect(vm.flowNodes).toHaveLength(1)
    expect(announcement(wrapper)).toContain('Agent node added')
  })

  it('runs every palette command, including the unknown-command default', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.rawNodes = [{ id: 'a', node_type: 'agent', position: { x: 0, y: 0 } }]
    vm.flowNodes = [{ id: 'a', position: { x: 0, y: 0 } }]
    vm.rawEdges = []
    vm.flowEdges = []
    vm.selectedNodeData = null
    vm.selectedEdgeData = null

    vm.runCanvasCommand('connect')
    await nextTick()
    expect(announcement(wrapper)).toContain('Select a node')

    vm.runCanvasCommand('arrange')
    await nextTick()
    expect(announcement(wrapper)).toContain('Graph arranged')

    vm.runCanvasCommand('toggle-hitl')
    await nextTick()
    expect(announcement(wrapper)).toContain('Select an edge')

    vm.runCanvasCommand('timeline')
    await nextTick()
    expect(vm.showVersionTimeline).toBe(true)

    expect(() => vm.runCanvasCommand('not-a-command')).not.toThrow()
  })

  it('navigates the palette with the arrow keys and tolerates an empty result set', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    keydown(canvasOf(wrapper), 'k', { ctrlKey: true })
    await nextTick()

    const search = wrapper.find('[data-testid="pipeline-editor-command-palette-input"]').element as HTMLInputElement
    const count = vm.filteredCanvasCommands.length

    keydown(search, 'ArrowDown')
    expect(vm.canvasPaletteIndex).toBe(1)
    keydown(search, 'ArrowUp')
    expect(vm.canvasPaletteIndex).toBe(0)
    keydown(search, 'ArrowUp')
    expect(vm.canvasPaletteIndex).toBe(count - 1)

    search.value = 'zzz-no-match'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    keydown(search, 'ArrowDown')
    expect(vm.canvasPaletteIndex).toBe(0)
    keydown(search, 'ArrowUp')
    expect(vm.canvasPaletteIndex).toBe(0)
    keydown(search, 'a')
    keydown(search, 'Enter')
    // No command selected: the palette must stay open rather than run one.
    expect(palette(wrapper).exists()).toBe(true)
  })

  it('ignores canvas keys while the palette is open and clears a selected node on Escape', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    const canvas = canvasOf(wrapper)

    vm.canvasPaletteOpen = true
    keydown(canvas, 'ArrowRight')
    expect(vm.canvasPaletteIndex).toBe(0)
    vm.canvasPaletteOpen = false

    vm.selectedNodeData = null
    vm.selectedEdgeData = null
    keydown(canvas, 'Escape')
    await nextTick()
    expect(vm.selectedNodeData).toBeNull()

    vm.selectedNodeData = { id: 'a', node_type: 'agent' }
    await nextTick()
    keydown(canvas, 'Escape')
    await nextTick()
    expect(vm.selectedNodeData).toBeNull()
    expect(document.activeElement).toBe(canvas)

    keydown(canvas, 'Enter')
    expect(vm.keyboardConnectSourceId).toBeNull()

    // A pending connection with nothing focused cannot be completed.
    vm.keyboardConnectSourceId = 'a'
    keydown(canvas, 'Enter')
    expect(vm.keyboardConnectSourceId).toBe('a')
  })

  it('ignores unrelated keys that are not canvas shortcuts', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    const canvas = canvasOf(wrapper)
    vm.canvasPaletteOpen = false
    vm.selectedNodeData = null
    vm.selectedEdgeData = null

    expect(() => keydown(canvas, 'a')).not.toThrow()
    await nextTick()
    expect(vm.canvasPaletteOpen).toBe(false)
    expect(vm.keyboardConnectSourceId).toBeNull()
  })

  it('completes a pending keyboard connection when Enter hits a focused node', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'a', node_type: 'agent', position: { x: 0, y: 0 } },
      { id: 'b', node_type: 'agent', position: { x: 200, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
    ]
    vm.rawEdges = []
    vm.flowEdges = []

    const canvas = canvasOf(wrapper)
    const target = document.createElement('div')
    target.className = 'vue-flow__node'
    target.setAttribute('data-id', 'b')
    target.tabIndex = 0
    canvas.appendChild(target)
    target.focus()
    vm.keyboardConnectSourceId = 'a'

    keydown(canvas, 'Enter')
    await nextTick()

    expect(vm.rawEdges).toHaveLength(1)
    expect(vm.keyboardConnectSourceId).toBeNull()
    expect(announcement(wrapper)).toContain('Connection created')
  })

  it('focuses the first node when an arrow is pressed with nothing focused', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.selectedNodeData = null
    vm.selectedEdgeData = null
    vm.flowNodes = [{ id: 'a', position: { x: 0, y: 0 } }]

    const canvas = canvasOf(wrapper)
    const node = document.createElement('div')
    node.className = 'vue-flow__node'
    node.setAttribute('data-id', 'a')
    node.tabIndex = 0
    canvas.appendChild(node)
    ;(document.activeElement as HTMLElement | null)?.blur?.()

    keydown(canvas, 'ArrowRight')
    await nextTick()
    expect(document.activeElement).toBe(node)
  })

  it('does nothing on an arrow when there is no node to focus at all', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.selectedNodeData = null
    vm.selectedEdgeData = null
    vm.flowNodes = []
    ;(document.activeElement as HTMLElement | null)?.blur?.()

    expect(() => keydown(canvasOf(wrapper), 'ArrowRight')).not.toThrow()
  })

  it('moves focus from the selected node and stops when no neighbour lies that way', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
    ]
    vm.selectedNodeData = { id: 'a', node_type: 'agent' }

    const canvas = canvasOf(wrapper)
    const a = document.createElement('div')
    a.className = 'vue-flow__node'
    a.setAttribute('data-id', 'a')
    a.tabIndex = 0
    const b = document.createElement('div')
    b.className = 'vue-flow__node'
    b.setAttribute('data-id', 'b')
    b.tabIndex = 0
    canvas.append(a, b)
    ;(document.activeElement as HTMLElement | null)?.blur?.()

    keydown(canvas, 'ArrowRight')
    await nextTick()
    expect(document.activeElement).toBe(b)

    keydown(canvas, 'ArrowRight')
    await nextTick()
    expect(document.activeElement).toBe(b)
  })

  it('picks the nearest node per direction and handles missing positions', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
      { id: 'c', position: { x: 0, y: 200 } },
      { id: 'd', position: { x: -200, y: 0 } },
      { id: 'no-position' },
    ]

    expect(vm.nodeIdInDirection('missing', 'ArrowRight')).toBeNull()
    expect(vm.nodeIdInDirection('a', 'ArrowRight')).toBe('b')
    expect(vm.nodeIdInDirection('a', 'ArrowLeft')).toBe('d')
    expect(vm.nodeIdInDirection('a', 'ArrowDown')).toBe('c')
    expect(vm.nodeIdInDirection('a', 'ArrowUp')).toBeNull()
    // b's only horizontal neighbour sits behind it — no forward progress.
    expect(vm.nodeIdInDirection('b', 'ArrowRight')).toBeNull()

    vm.flowNodes.push({ id: 'closer', position: { x: 100, y: 0 } })
    expect(vm.nodeIdInDirection('a', 'ArrowRight')).toBe('closer')
    // A farther candidate after the best one does not replace it.
    vm.flowNodes.push({ id: 'farther', position: { x: 400, y: 0 } })
    expect(vm.nodeIdInDirection('a', 'ArrowRight')).toBe('closer')
  })

  it('refuses self-connections and keeps an existing flow edge', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'a', node_type: 'agent', position: { x: 0, y: 0 } },
      { id: 'b', node_type: 'agent', position: { x: 200, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
    ]
    vm.rawEdges = []
    vm.flowEdges = [{ id: 'f1', source: 'a', target: 'b' }]

    vm.connectNodes('a', 'a')
    expect(vm.rawEdges).toHaveLength(0)

    vm.connectNodes('a', 'b')
    expect(vm.rawEdges).toHaveLength(1)
    expect(vm.flowEdges).toHaveLength(1)
  })

  it('nudges horizontally and vertically, and ignores unknown or missing nodes', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'a', node_type: 'agent', position: { x: 100, y: 100 } },
      { id: 'no-position' },
    ]
    vm.flowNodes = [
      { id: 'a', position: { x: 100, y: 100 } },
      { id: 'no-position' },
    ]

    const canvas = canvasOf(wrapper)
    const node = document.createElement('div')
    node.className = 'vue-flow__node'
    node.setAttribute('data-id', 'a')
    node.tabIndex = 0
    canvas.appendChild(node)
    node.focus()

    keydown(canvas, 'ArrowLeft', { shiftKey: true })
    await nextTick()
    expect(vm.flowNodes[0].position).toEqual({ x: 80, y: 100 })

    keydown(canvas, 'ArrowUp', { shiftKey: true })
    await nextTick()
    expect(vm.flowNodes[0].position).toEqual({ x: 80, y: 80 })

    // A selected node that is absent from both graphs moves nothing.
    ;(document.activeElement as HTMLElement | null)?.blur?.()
    vm.selectedNodeData = { id: 'ghost', node_type: 'agent' }
    vm.nudgeFocusedNode('ArrowRight')
    expect(vm.flowNodes[0].position).toEqual({ x: 80, y: 80 })

    // No focused node and no selection: nothing to nudge.
    vm.selectedNodeData = null
    expect(() => vm.nudgeFocusedNode('ArrowRight')).not.toThrow()
  })

  it('disables a HITL gate that was already enabled', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.selectedEdgeData = { id: 'e1' }
    vm.edgeForm.hitl_enabled = true

    vm.toggleSelectedEdgeHitl()
    await nextTick()

    expect(vm.edgeForm.hitl_enabled).toBe(false)
    expect(announcement(wrapper)).toContain('HITL gate disabled')
  })

  it('classifies editable targets so shortcuts never fire while typing', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any

    expect(vm.isTypingTarget(null)).toBe(false)
    expect(vm.isTypingTarget(document.createTextNode('x'))).toBe(false)
    expect(vm.isTypingTarget(document.createElement('input'))).toBe(true)
    expect(vm.isTypingTarget(document.createElement('textarea'))).toBe(true)
    expect(vm.isTypingTarget(document.createElement('select'))).toBe(true)
    const editable = document.createElement('div')
    // jsdom does not implement isContentEditable; model the browser state the
    // guard reads so the contenteditable arm is exercised.
    Object.defineProperty(editable, 'isContentEditable', { value: true })
    expect(vm.isTypingTarget(editable)).toBe(true)
    expect(vm.isTypingTarget(document.createElement('div'))).toBe(false)
  })

  it('returns null when the canvas ref is not bound', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    const canvas = vm.canvasRef
    vm.canvasRef = null
    expect(vm.canvasNodeElement('a')).toBeNull()
    vm.canvasRef = canvas
  })

  it('completes or ignores a pending keyboard connection from a node click', async () => {
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    vm.rawNodes = [
      { id: 'a', node_type: 'agent', position: { x: 0, y: 0 } },
      { id: 'b', node_type: 'agent', position: { x: 200, y: 0 } },
    ]
    vm.flowNodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 200, y: 0 } },
    ]
    vm.rawEdges = []
    vm.flowEdges = []

    vm.keyboardConnectSourceId = 'a'
    vm.onNodeClick({ node: { id: 'b' } })
    await nextTick()
    expect(vm.rawEdges).toHaveLength(1)
    expect(vm.keyboardConnectSourceId).toBeNull()

    // Clicking the source node itself keeps the pending connection.
    vm.keyboardConnectSourceId = 'a'
    vm.onNodeClick({ node: { id: 'a' } })
    await nextTick()
    expect(vm.rawEdges).toHaveLength(1)
    expect(vm.keyboardConnectSourceId).toBe('a')
  })

  it('offers only read commands and refuses every canvas write in the historic view', async () => {
    const { useRoute } = await import('vue-router')
    const route = useRoute() as unknown as { query: Record<string, string> }
    route.query = { snapshot: 'v1' }
    try {
      const wrapper = await mountedEditor()
      const vm = wrapper.vm as any
      vm.viewSnapshot = { id: 'v1', snapshot_version: 1 }
      vm.viewSnapshotId = 'v1'
      await nextTick()

      expect(vm.isViewing).toBe(true)
      expect(vm.canvasCommands.map((command: any) => command.id)).toEqual(['fit', 'timeline'])

      vm.startKeyboardConnect()
      expect(vm.keyboardConnectSourceId).toBeNull()

      vm.rawEdges = []
      vm.connectNodes('a', 'b')
      expect(vm.rawEdges).toHaveLength(0)

      vm.selectedNodeData = { id: 'a', node_type: 'agent' }
      vm.nudgeFocusedNode('ArrowRight')
      expect(vm.flowNodes).toHaveLength(0)

      vm.selectedEdgeData = { id: 'e1' }
      const before = vm.edgeForm.hitl_enabled
      vm.toggleSelectedEdgeHitl()
      expect(vm.edgeForm.hitl_enabled).toBe(before)

      vm.keyboardConnectSourceId = 'a'
      vm.onNodeClick({ node: { id: 'b' } })
      expect(vm.keyboardConnectSourceId).toBe('a')
    } finally {
      route.query = {}
    }
  })
})

describe('PipelineEditorView mobile sheet focus return (FAR-2464)', () => {
  it('returns focus to the originating graph node, or the canvas when it is gone', async () => {
    setMatchMedia(false)
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any
    const canvas = canvasOf(wrapper)
    const node = document.createElement('div')
    node.className = 'vue-flow__node'
    node.setAttribute('data-id', 'n1')
    node.tabIndex = 0
    canvas.appendChild(node)

    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    vm.selectedNodeData = null
    await nextTick()
    expect(document.activeElement).toBe(node)

    // A node that no longer exists in the canvas falls back to the remembered
    // focus target (the canvas itself) when the trigger was body/canvas.
    ;(document.activeElement as HTMLElement | null)?.blur?.()
    vm.selectedNodeData = { id: 'ghost', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()
    vm.selectedNodeData = null
    await nextTick()
    expect(document.activeElement).toBe(canvas)
  })

  it('focuses the panel itself when it has no focusable control', async () => {
    setMatchMedia(false)
    const wrapper = await mountedEditor()
    const vm = wrapper.vm as any

    vm.selectedNodeData = { id: 'n1', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    const panel = wrapper.find('aside').element as HTMLElement
    vi.spyOn(panel, 'querySelector').mockReturnValue(null)
    ;(document.activeElement as HTMLElement | null)?.blur?.()

    vm.selectedNodeData = { id: 'n2', node_type: 'agent', agent_id: 'agent-1' }
    await nextTick()

    expect(document.activeElement).toBe(panel)
  })
})
