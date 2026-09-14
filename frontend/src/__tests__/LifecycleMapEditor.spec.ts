import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent } from 'vue'

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  exitToLogin: vi.fn(),
}))

import LifecycleMapEditor from '../components/lifecycle-map/editor/LifecycleMapEditor.vue'
import VersionHistoryDropdown from '../components/lifecycle-map/editor/VersionHistoryDropdown.vue'
import { formatApiError } from '../lib/api/formatError'
import { useApi } from '../composables/useApi'

const stageA = {
  id: 'stage-a',
  name: 'Stage A',
  description: '',
  stage_type: 'modulo',
  pipeline_id: null,
  external_url: null,
  owner: null,
  graduated: false,
}
const stageB = {
  id: 'stage-b',
  name: 'Stage B',
  description: '',
  stage_type: 'manual',
  pipeline_id: null,
  external_url: null,
  owner: null,
  graduated: false,
}
const edge = {
  id: 'edge-1',
  source_stage_id: 'stage-a',
  target_stage_id: 'stage-b',
  trigger_type: 'pipeline_completed',
  description: '',
  condition_expression: null,
  estimated_frequency: null,
  trigger_link: null,
}

const versions = [
  { id: 'ver-1', lifecycle_map_id: 'map-1', version_number: 1, stages: [stageA], edges: [edge], created_by: 'alice', created_at: '2026-01-01T00:00:00Z', notes: '' },
  { id: 'ver-2', lifecycle_map_id: 'map-1', version_number: 2, stages: [stageB], edges: [], created_by: 'alice', created_at: '2026-01-02T00:00:00Z', notes: '' },
]

const putMock = vi.fn()
const postMock = vi.fn()

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn((url: string) => {
      if (url.endsWith('/versions')) return Promise.resolve(versions)
      if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
      return Promise.resolve({ id: 'map-1', name: 'Launch Flow' })
    }),
    post: postMock,
    put: putMock,
  })),
}))

const VueFlowStub = defineComponent({
  name: 'VueFlow',
  props: {
    nodes: { type: Array, default: () => [] },
    edges: { type: Array, default: () => [] },
  },
  template: '<div data-testid="vue-flow-stub" />',
})

function mountEditor() {
  return mount(LifecycleMapEditor, {
    props: { mapId: 'map-1' },
    global: {
      stubs: {
        VueFlow: VueFlowStub,
        Background: true,
        Controls: true,
        StagePalette: true,
        StageConfigPanel: true,
        EdgeConfigPanel: true,
        GraduationDialog: true,
        Button: true,
      },
    },
  })
}

describe('LifecycleMapEditor version loading', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads the selected version content into the canvas', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const flow = wrapper.findComponent(VueFlowStub)
    const nodes = flow.props('nodes') as { data: { name: string } }[]
    expect(nodes).toHaveLength(1)
    expect(nodes[0].data.name).toBe('Stage A')

    const dropdown = wrapper.findComponent(VersionHistoryDropdown)
    dropdown.vm.$emit('select', 'ver-2')
    await flushPromises()

    const nodesAfter = flow.props('nodes') as { data: { name: string } }[]
    const edgesAfter = flow.props('edges') as unknown[]
    expect(nodesAfter).toHaveLength(1)
    expect(nodesAfter[0].data.name).toBe('Stage B')
    expect(edgesAfter).toHaveLength(0)
  })

  it('switches the current version id to the loaded version', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const dropdown = wrapper.findComponent(VersionHistoryDropdown)
    expect(dropdown.props('currentVersionId')).toBe('ver-1')

    dropdown.vm.$emit('select', 'ver-2')
    await flushPromises()

    expect(wrapper.findComponent(VersionHistoryDropdown).props('currentVersionId')).toBe('ver-2')

    const flow = wrapper.findComponent(VueFlowStub)
    const nodes = flow.props('nodes') as { data: { name: string } }[]
    const edges = flow.props('edges') as unknown[]
    expect(nodes).toHaveLength(1)
    expect(nodes[0].data.name).toBe('Stage B')
    expect(edges).toHaveLength(0)
  })

  it('surfaces the backend 422 validation detail in saveError', async () => {
    // FastAPI returns a Pydantic 422 as { detail: [{loc, msg, type}, ...] };
    // useApi collapses it to readable text and rejects with an Error whose
    // message is that text. The editor must surface the real Error message
    // rather than the raw "[object Object]" the API error body would stringify to.
    const validationDetail = { detail: [
      { loc: ['body', 'stages', 1, 'id'], msg: 'lifecycle-map stage #1: duplicate stage id', type: 'value_error' },
    ] }
    putMock.mockRejectedValueOnce(new Error(formatApiError(validationDetail)))

    const wrapper = mountEditor()
    await flushPromises()

    await (wrapper.vm as unknown as { handleSave: () => Promise<void> }).handleSave()
    await flushPromises()

    expect(wrapper.text()).toContain('lifecycle-map stage #1: duplicate stage id')
    expect(wrapper.text()).not.toContain('"detail"')
  })

  it('formats FastAPI array-typed 422 detail into readable messages', () => {
    expect(
      formatApiError({
        detail: [
          { loc: ['body', 'name'], msg: 'String should have at least 1 character', type: 'string_too_short' },
          { loc: ['body', 'visibility'], msg: 'String should match pattern', type: 'string_pattern_mismatch' },
        ],
      }),
    ).toBe('String should have at least 1 character; String should match pattern')
  })
})

/* ── save, selection, edges, fields, layout, empty/error states ───────── */

describe('LifecycleMapEditor interactions', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('calls PUT when saving with an existing version', async () => {
    putMock.mockResolvedValueOnce({ id: 'ver-1' })
    const wrapper = mountEditor()
    await flushPromises()

    await (wrapper.vm as unknown as { handleSave: () => Promise<void> }).handleSave()
    await flushPromises()

    expect(putMock).toHaveBeenCalledOnce()
    const [url, body] = putMock.mock.calls[0] as [string, { stages: unknown[]; edges: unknown[]; notes: string }]
    expect(url).toContain('/versions/ver-1')
    expect(body.stages).toHaveLength(1)
    expect(body.stages[0]).toMatchObject({ id: 'stage-a', name: 'Stage A' })
    expect(body.edges).toHaveLength(1)
    expect(body.edges[0]).toMatchObject({ id: 'edge-1', source_stage_id: 'stage-a' })
    expect(body.notes).toContain('1 stages')
  })

  it('calls POST when saving without a version id', async () => {
    postMock.mockResolvedValueOnce({ id: 'ver-new', lifecycle_map_id: 'map-1', version_number: 3, stages: [], edges: [], created_by: 'bob', created_at: '', notes: '' })
    vi.mocked(useApi).mockReturnValueOnce({
      get: vi.fn((url: string) => {
        if (url.endsWith('/versions')) return Promise.resolve([])
        if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
        return Promise.resolve({ id: 'map-1', name: 'Empty Map' })
      }),
      post: postMock,
      put: putMock,
    } as unknown as ReturnType<typeof useApi>)

    const wrapper = mountEditor()
    await flushPromises()

    // Clear version id so handleSave takes the POST branch
    ;(wrapper.vm as unknown as { currentVersionId: string }).currentVersionId = ''

    await (wrapper.vm as unknown as { handleSave: () => Promise<void> }).handleSave()
    await flushPromises()

    expect(postMock).toHaveBeenCalledOnce()
    expect(putMock).not.toHaveBeenCalled()
    const [url] = postMock.mock.calls[0] as [string]
    expect(url).toContain('/versions')
  })

  it('emits saved after a successful save', async () => {
    putMock.mockResolvedValueOnce({})
    const wrapper = mountEditor()
    await flushPromises()

    await (wrapper.vm as unknown as { handleSave: () => Promise<void> }).handleSave()
    await flushPromises()

    expect(wrapper.emitted('saved')).toHaveLength(1)
  })

  /* ── selection ─────────────────────────────────────────────────────── */

  it('selects a node on onNodeClick and clears edge selection', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const node = { id: 'stage-a', data: { name: 'Stage A' } }
    ;(wrapper.vm as unknown as { onNodeClick: (e: { node: unknown }) => void }).onNodeClick({ node })

    expect((wrapper.vm as unknown as { selectedNode: unknown }).selectedNode).toEqual(node)
    expect((wrapper.vm as unknown as { selectedEdge: unknown }).selectedEdge).toBeNull()
  })

  it('selects an edge on onEdgeClick and clears node selection', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const edgeObj = { id: 'edge-1', data: { trigger_type: 'pipeline_completed' } }
    ;(wrapper.vm as unknown as { onEdgeClick: (e: { edge: unknown }) => void }).onEdgeClick({ edge: edgeObj })

    expect((wrapper.vm as unknown as { selectedEdge: unknown }).selectedEdge).toEqual(edgeObj)
    expect((wrapper.vm as unknown as { selectedNode: unknown }).selectedNode).toBeNull()
  })

  it('clears all selection on onPaneClick', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const node = { id: 'stage-a', data: { name: 'Stage A' } }
    ;(wrapper.vm as unknown as { onNodeClick: (e: { node: unknown }) => void }).onNodeClick({ node })
    expect((wrapper.vm as unknown as { selectedNode: unknown }).selectedNode).not.toBeNull()

    ;(wrapper.vm as unknown as { onPaneClick: () => void }).onPaneClick()
    expect((wrapper.vm as unknown as { selectedNode: unknown }).selectedNode).toBeNull()
    expect((wrapper.vm as unknown as { selectedEdge: unknown }).selectedEdge).toBeNull()
  })

  it('clears selection when switching versions', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const node = { id: 'stage-a', data: { name: 'Stage A' } }
    ;(wrapper.vm as unknown as { onNodeClick: (e: { node: unknown }) => void }).onNodeClick({ node })
    expect((wrapper.vm as unknown as { selectedNode: unknown }).selectedNode).not.toBeNull()

    wrapper.findComponent(VersionHistoryDropdown).vm.$emit('select', 'ver-2')
    await flushPromises()

    expect((wrapper.vm as unknown as { selectedNode: unknown }).selectedNode).toBeNull()
    expect((wrapper.vm as unknown as { selectedEdge: unknown }).selectedEdge).toBeNull()
  })

  /* ── edge / node creation ─────────────────────────────────────────── */

  it('adds a new edge via onConnect', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const before = ((wrapper.vm as unknown as { flowEdges: unknown[] }).flowEdges).length

    ;(wrapper.vm as unknown as { onConnect: (c: { source: string; target: string }) => void }).onConnect({
      source: 'stage-a',
      target: 'stage-b',
    })

    const after = ((wrapper.vm as unknown as { flowEdges: unknown[] }).flowEdges).length
    expect(after).toBe(before + 1)

    const newEdge = (wrapper.vm as unknown as { flowEdges: Array<{ source: string; target: string; data: { trigger_type: string } }> }).flowEdges[after - 1]
    expect(newEdge.source).toBe('stage-a')
    expect(newEdge.target).toBe('stage-b')
    expect(newEdge.data.trigger_type).toBe('pipeline_completed')
  })

  /* ── field updates ─────────────────────────────────────────────────── */

  it('updates node data via onStageFieldUpdate', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const node = { id: 'stage-a', data: { name: 'Stage A', description: '' } }
    ;(wrapper.vm as unknown as { onNodeClick: (e: { node: unknown }) => void }).onNodeClick({ node })

    ;(wrapper.vm as unknown as { onStageFieldUpdate: (f: string, v: unknown) => void }).onStageFieldUpdate('name', 'Renamed')

    const updated = (wrapper.vm as unknown as { flowNodes: Array<{ id: string; data: { name: string } }> }).flowNodes.find((n) => n.id === 'stage-a')
    expect(updated!.data.name).toBe('Renamed')
  })

  it('updates edge data via onEdgeFieldUpdate', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const edgeObj = { id: 'edge-1', data: { trigger_type: 'pipeline_completed', description: '' } }
    ;(wrapper.vm as unknown as { onEdgeClick: (e: { edge: unknown }) => void }).onEdgeClick({ edge: edgeObj })

    ;(wrapper.vm as unknown as { onEdgeFieldUpdate: (f: string, v: unknown) => void }).onEdgeFieldUpdate('description', 'New desc')

    const updated = (wrapper.vm as unknown as { flowEdges: Array<{ id: string; data: { description: string } }> }).flowEdges.find((e) => e.id === 'edge-1')
    expect(updated!.data.description).toBe('New desc')
  })

  it('onStageFieldUpdate is a no-op when nothing selected', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const snap = JSON.stringify((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes)
    ;(wrapper.vm as unknown as { onStageFieldUpdate: (f: string, v: unknown) => void }).onStageFieldUpdate('name', 'Nope')
    expect(JSON.stringify((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes)).toBe(snap)
  })

  it('onEdgeFieldUpdate is a no-op when nothing selected', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const snap = JSON.stringify((wrapper.vm as unknown as { flowEdges: unknown[] }).flowEdges)
    ;(wrapper.vm as unknown as { onEdgeFieldUpdate: (f: string, v: unknown) => void }).onEdgeFieldUpdate('description', 'Nope')
    expect(JSON.stringify((wrapper.vm as unknown as { flowEdges: unknown[] }).flowEdges)).toBe(snap)
  })

  /* ── auto layout ───────────────────────────────────────────────────── */

  it('autoLayout recomputes node positions without error', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const nodes = (wrapper.vm as unknown as { flowNodes: Array<{ position: { x: number; y: number } }> }).flowNodes
    expect(nodes.length).toBeGreaterThan(0)
    ;(wrapper.vm as unknown as { autoLayout: () => void }).autoLayout()

    expect(typeof nodes[0].position.x).toBe('number')
    expect(typeof nodes[0].position.y).toBe('number')
  })

  it('autoLayout does nothing on empty canvas', async () => {
    vi.mocked(useApi).mockReturnValueOnce({
      get: vi.fn((url: string) => {
        if (url.endsWith('/versions')) return Promise.resolve([])
        if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
        return Promise.resolve({ id: 'map-1', name: 'Empty' })
      }),
      post: postMock,
      put: putMock,
    } as unknown as ReturnType<typeof useApi>)

    const wrapper = mountEditor()
    await flushPromises()

    // Should not throw
    ;(wrapper.vm as unknown as { autoLayout: () => void }).autoLayout()
    expect((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes).toHaveLength(0)
  })

  /* ── page error / loading / empty states ───────────────────────────── */

  it('shows page error when get() rejects', async () => {
    vi.mocked(useApi).mockReturnValueOnce({
      get: vi.fn().mockRejectedValue(new Error('Network error')),
      post: postMock,
      put: putMock,
    } as unknown as ReturnType<typeof useApi>)

    const wrapper = mountEditor()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to load')
  })

  it('shows page error when map data is null (404)', async () => {
    vi.mocked(useApi).mockReturnValueOnce({
      get: vi.fn((url: string) => {
        if (url.endsWith('/versions')) return Promise.resolve([])
        if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
        return Promise.resolve(null)
      }),
      post: postMock,
      put: putMock,
    } as unknown as ReturnType<typeof useApi>)

    const wrapper = mountEditor()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to load lifecycle map')
  })

  it('renders empty canvas when version list is empty', async () => {
    vi.mocked(useApi).mockReturnValueOnce({
      get: vi.fn((url: string) => {
        if (url.endsWith('/versions')) return Promise.resolve([])
        if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
        return Promise.resolve({ id: 'map-1', name: 'Empty Map' })
      }),
      post: postMock,
      put: putMock,
    } as unknown as ReturnType<typeof useApi>)

    const wrapper = mountEditor()
    await flushPromises()

    const flow = wrapper.findComponent(VueFlowStub)
    expect(flow.props('nodes')).toHaveLength(0)
    expect(flow.props('edges')).toHaveLength(0)
  })

  it('displays map name in toolbar', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    expect(wrapper.text()).toContain('Launch Flow')
  })

  it('passes all versions to the dropdown', async () => {
    const wrapper = mountEditor()
    await flushPromises()
    const dropdown = wrapper.findComponent(VersionHistoryDropdown)
    expect(dropdown.props('versions')).toHaveLength(2)
  })

  /* ── onDragOver ────────────────────────────────────────────────────── */

  it('onDragOver sets dropEffect to copy', async () => {
    // jsdom lacks DragEvent — stub a minimal one
    class FakeDragEvent extends Event {
      dataTransfer: { dropEffect: string } | null = null
      constructor(type: string, init?: { bubbles?: boolean; dataTransfer?: { dropEffect: string } }) {
        super(type, { bubbles: init?.bubbles })
        this.dataTransfer = init?.dataTransfer ?? null
      }
    }
    vi.stubGlobal('DragEvent', FakeDragEvent)

    const wrapper = mountEditor()
    await flushPromises()

    const dt = { dropEffect: '' }
    const event = new FakeDragEvent('dragover', { bubbles: true, dataTransfer: dt })
    ;(wrapper.vm as unknown as { onDragOver: (e: unknown) => void }).onDragOver(event)

    expect(dt.dropEffect).toBe('copy')
    vi.unstubAllGlobals()
  })

  it('onDragOver is a no-op for non-DragEvent', async () => {
    class FakeDragEvent extends Event {}
    vi.stubGlobal('DragEvent', FakeDragEvent)

    const wrapper = mountEditor()
    await flushPromises()
    // Should not throw — {} is not instanceof DragEvent
    ;(wrapper.vm as unknown as { onDragOver: (e: unknown) => void }).onDragOver({})
    vi.unstubAllGlobals()
  })

  /* ── onDrop ─────────────────────────────────────────────────────────── */

  it('onDrop ignores non-DragEvent', async () => {
    class FakeDragEvent extends Event {}
    vi.stubGlobal('DragEvent', FakeDragEvent)

    const wrapper = mountEditor()
    await flushPromises()

    const before = ((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes).length
    ;(wrapper.vm as unknown as { onDrop: (e: unknown) => void }).onDrop({})
    expect(((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes).length).toBe(before)
    vi.unstubAllGlobals()
  })

  it('onDrop ignores DragEvent with no lifecycle-stage type', async () => {
    class FakeDragEvent extends Event {
      dataTransfer: { getData: ReturnType<typeof vi.fn>; dropEffect: string } | null = null
      constructor(type: string, init?: { bubbles?: boolean; dataTransfer?: { getData: ReturnType<typeof vi.fn>; dropEffect: string } }) {
        super(type, { bubbles: init?.bubbles })
        this.dataTransfer = init?.dataTransfer ?? null
      }
    }
    vi.stubGlobal('DragEvent', FakeDragEvent)

    const wrapper = mountEditor()
    await flushPromises()

    const dt = { getData: vi.fn(() => ''), dropEffect: '' }
    const event = new FakeDragEvent('drop', { bubbles: true, dataTransfer: dt })

    const before = ((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes).length
    ;(wrapper.vm as unknown as { onDrop: (e: unknown) => void }).onDrop(event)
    expect(((wrapper.vm as unknown as { flowNodes: unknown[] }).flowNodes).length).toBe(before)
    vi.unstubAllGlobals()
  })
})
