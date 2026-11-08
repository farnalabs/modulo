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
